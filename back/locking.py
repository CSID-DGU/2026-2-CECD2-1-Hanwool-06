"""Cross-platform advisory locks for local collection processes."""
import errno
import os
import time


def lock_file(file, *, blocking=True):
    if os.name != "nt":
        import fcntl
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(file, flags)
        return
    import msvcrt
    while True:
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EDEADLOCK):
                raise
            if not blocking:
                raise BlockingIOError("Collection lock is held") from exc
            time.sleep(0.1)


def unlock_file(file):
    if os.name != "nt":
        import fcntl
        fcntl.flock(file, fcntl.LOCK_UN)
        return
    import msvcrt
    file.seek(0)
    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
