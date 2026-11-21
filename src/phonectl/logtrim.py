"""Keeps the service's log files from growing without end.

launchd opens the files for the server's stdout and stderr (for appending) and hands them over,
so the server cannot reopen them under a new name. Instead, when one grows past MAX_BYTES, the
server copies it to <file>.1, replacing the older copy, and empties it in place; later writes
are appends, so they start again at the top. At most about twice MAX_BYTES per file is kept.
Lines written between the copy and the emptying are lost; that is a few milliseconds.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import stat
import threading
import time

MAX_BYTES = 10 * 1024 * 1024
CHECK_S = 600.0

log = logging.getLogger(__name__)


def _path(fd: int) -> str | None:
    """The file an open descriptor refers to (macOS: F_GETPATH)."""
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    except (OSError, AttributeError):
        return None
    return os.fsdecode(raw.split(b"\0", 1)[0]) or None


def trim(fd: int, max_bytes: int = MAX_BYTES) -> bool:
    """If fd is a regular file larger than max_bytes, keep its contents as <file>.1 and empty
    it. True if it did. A terminal or pipe (the stdio server's stderr) is left alone."""
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size <= max_bytes:
        return False
    path = _path(fd)
    if path is None:
        return False
    shutil.copyfile(path, path + ".1")
    os.ftruncate(fd, 0)
    return True


def keep_trimmed(fds: tuple[int, ...] = (1, 2), every_s: float = CHECK_S) -> None:
    """Check the files behind fds now and every every_s seconds, in a background thread."""
    def loop() -> None:
        while True:
            for fd in fds:
                try:
                    if trim(fd):
                        log.info("log over %d MB: older lines moved to %s.1", MAX_BYTES >> 20, _path(fd))
                except OSError as e:  # a full disk or a removed file must not stop the server
                    log.warning("could not trim the log on fd %d: %s", fd, e)
            time.sleep(every_s)

    threading.Thread(target=loop, name="log-trim", daemon=True).start()
