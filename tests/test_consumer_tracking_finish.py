"""Finite finish drains the one existing worker call and rejects new callbacks."""

import asyncio
import threading

from test_consumer_tracking_integration import configure, send
from tracking_fixtures import scope as scope


def test_finish_preserves_pending_future_and_stops_additional_batches(scope, monkeypatch):
    import scipy.optimize
    real = scipy.optimize.linear_sum_assignment
    entered, release = threading.Event(), threading.Event()

    def blocked(matrix):
        entered.set()
        assert release.wait(2.)
        return real(matrix)

    async def scenario():
        consumer, catalog, _, clock, _ = configure(scope)
        try:
            await send(consumer, clock)
            monkeypatch.setattr(scipy.optimize, 'linear_sum_assignment', blocked)
            task = asyncio.create_task(send(consumer, clock, .3))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            owned = consumer._call
            finishing = asyncio.create_task(consumer.finish())
            await asyncio.sleep(.01)
            assert consumer._call is owned and not owned.done() and not finishing.done()
            await send(consumer, clock, .31)
            assert consumer._call is owned and consumer._tracking_frames == 2
            release.set()
            await task
            result = await finishing
            assert result.status.state == 'ready' and len(consumer._feature_message.observations) == 4
            assert not consumer._tracker._tracks and not consumer._feature_sink._features
        finally:
            release.set()
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())
