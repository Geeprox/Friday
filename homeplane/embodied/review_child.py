"""Private inference lease released only after the owned child actually exits."""

import fcntl
import os
import stat
from pathlib import Path

from .local_child import LocalChild


class ReviewLease:
    def __init__(self, private, name, *, busy, unavailable):
        self.private, self.name = Path(private), name
        self.busy, self.unavailable = busy, unavailable
        self.descriptor = None

    def acquire(self, *, blocking=False):
        assert blocking is False
        directory = descriptor = None
        try:
            self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = os.open(self.private, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError(self.unavailable)
            descriptor = os.open(
                self.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600, dir_fd=directory,
            )
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size != 0):
                raise ValueError(self.unavailable)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(self.busy) from None
            self.descriptor, descriptor = descriptor, None
            return True
        except OSError:
            raise ValueError(self.unavailable) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)

    def release(self):
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            os.close(descriptor)


class ReviewChild(LocalChild):
    def __init__(self, command, private, name, *, busy, unavailable, **options):
        lease = ReviewLease(private, name, busy=busy, unavailable=unavailable)
        super().__init__(command, lease, terminate_timeout=2, **options)

    @property
    def lease(self):
        return self.slots.descriptor

    def spawn_options(self):
        # The child retains this empty lock even if its parent crashes before
        # cleanup. Its owner watcher then exits and releases the last copy.
        return dict(super().spawn_options(), pass_fds=(self.lease,))
