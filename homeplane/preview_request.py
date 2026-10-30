"""Cancel bounded preview preparation when its local viewer disconnects."""

import asyncio
import inspect


async def prepare_for_viewer(request, operation, *, timeout=195, discard=None):
    """Keep rendering owned by the request, including cancellation and timeout.

    The body has already been validated by FastAPI. Only disconnection is polled;
    this helper never inspects, logs or retains household request contents.
    """
    task = asyncio.create_task(operation(), name="private-preview-request")
    delivered = False
    try:
        async with asyncio.timeout(timeout):
            while not task.done():
                await asyncio.wait({task}, timeout=0.2)
                if not task.done() and await request.is_disconnected():
                    raise ValueError("archive_preview_cancelled")
            result = task.result()
            if await request.is_disconnected():
                raise ValueError("archive_preview_cancelled")
            delivered = True
            return result
    finally:
        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Completion may race the disconnect. Revoke a finished preview
            # which was never transferred to the caller, as well as cancelling
            # an unfinished render. A later transport failure is bounded by TTL.
            if not delivered and discard and not task.cancelled() and task.exception() is None:
                value = discard(task.result())
                if inspect.isawaitable(value):
                    await value

        cleanup_task = asyncio.create_task(cleanup(), name="private-preview-request-cleanup")
        await asyncio.shield(cleanup_task)
