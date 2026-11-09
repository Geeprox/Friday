"""Small bounded, reprioritizable stage pools. No application/device work on import."""

import threading
from concurrent.futures import Future


class PriorityPool:
    def __init__(self, workers, name):
        self.condition = threading.Condition()
        self.pending = []
        self.ranks = {}
        self.sequence = 0
        self.closed = False
        self.threads = []
        self.name = name
        self.desired = workers
        self.resize(workers)

    def resize(self, workers):
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("invalid_workers")
        with self.condition:
            self.desired = workers
            while len(self.threads) < workers:
                number = len(self.threads)
                thread = threading.Thread(
                    target=self._run, args=(number,), name=self.name + str(number), daemon=True
                )
                self.threads.append(thread)
                thread.start()
            self.condition.notify_all()

    def submit(self, key, function):
        with self.condition:
            if self.closed:
                raise RuntimeError("stage_closed")
            future = Future()
            self.sequence += 1
            self.pending.append((key, self.sequence, function, future))
            self.condition.notify()
            return future

    def prioritize(self, ids):
        with self.condition:
            self.ranks = {key: at for at, key in enumerate(ids)}
            self.condition.notify_all()

    def _run(self, number):
        while True:
            with self.condition:
                while (not self.pending or number >= self.desired) and not self.closed:
                    self.condition.wait()
                if self.closed:
                    for item in self.pending:
                        item[3].cancel()
                    self.pending.clear()
                    return
                index = min(
                    range(len(self.pending)),
                    key=lambda i: (self.ranks.get(self.pending[i][0], 1000000), self.pending[i][1]),
                )
                key, sequence, function, future = self.pending.pop(index)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(function())
            except BaseException as error:
                future.set_exception(error)

    def shutdown(self, wait=True):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if wait:
            for thread in self.threads:
                thread.join()
