import os

from phonectl import logtrim


def _append(path):
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)  # as launchd opens the logs


def test_a_log_over_the_limit_moves_to_dot_1_and_starts_again(tmp_path):
    path = tmp_path / "phonectl.error.log"
    fd = _append(path)
    try:
        os.write(fd, b"old line\n" * 20)
        assert logtrim.trim(fd, max_bytes=100)
        os.write(fd, b"new line\n")
    finally:
        os.close(fd)
    assert path.read_bytes() == b"new line\n"
    assert (tmp_path / "phonectl.error.log.1").read_bytes() == b"old line\n" * 20


def test_a_log_under_the_limit_is_left_alone(tmp_path):
    path = tmp_path / "phonectl.log"
    fd = _append(path)
    try:
        os.write(fd, b"line\n")
        assert not logtrim.trim(fd, max_bytes=100)
    finally:
        os.close(fd)
    assert path.read_bytes() == b"line\n" and not (tmp_path / "phonectl.log.1").exists()


def test_a_pipe_is_left_alone():
    r, w = os.pipe()
    try:
        os.write(w, b"x" * 200)
        assert not logtrim.trim(w, max_bytes=100)
    finally:
        os.close(r)
        os.close(w)
