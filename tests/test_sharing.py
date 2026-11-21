"""Several clients on one server, over real HTTP, against a fake phone (tests/fake_phone.py).

Clients in separate processes are told apart by their process; clients in this process by
the name they send. The server runs with short idle and wait times so the tests stay quick.
"""

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from phonectl.client import Busy, Phone, ScreenChanged

HERE = Path(__file__).parent
IDLE_S = 1.0
WAIT_S = 3.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("srv")
    port = _free_port()
    env = {**os.environ, "PHONECTL_PORT": str(port), "PHONECTL_TOKEN_FILE": str(tmp / "token"),
           "PHONECTL_IDLE": str(IDLE_S), "PHONECTL_WAIT": str(WAIT_S), "PHONECTL_LOG": "INFO"}
    log = tmp / "inputs"
    proc = subprocess.Popen([sys.executable, str(HERE / "fake_phone.py"), str(log)], env=env,
                            stdout=subprocess.DEVNULL, stderr=open(tmp / "server.log", "w"))
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError((tmp / "server.log").read_text())

    class Srv:
        pass

    Srv.port, Srv.token, Srv.inputs, Srv.log, Srv.env = port, (tmp / "token").read_text().strip(), log, tmp / "server.log", env
    yield Srv
    proc.terminate()
    proc.wait(5)


@pytest.fixture(autouse=True)
def free_phone(srv):
    """Each test starts with the phone free: wait out whoever held it last."""
    yield
    time.sleep(IDLE_S + 0.1)


def _phone(srv, name):
    return Phone(name=name, port=srv.port, token=srv.token)


def _script(srv, body: str) -> subprocess.Popen:
    """A client in its own process; it prints what happened."""
    code = "from phonectl.client import *\nimport time\n" + textwrap.dedent(body)
    return subprocess.Popen([sys.executable, "-c", code], env=srv.env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_processes_take_turns(srv):
    a = _script(srv, f"""
        p = Phone(port={srv.port})
        p.read_text(); p.tap(10, 10); print("a tapped", flush=True)
        time.sleep(3)
    """)
    assert a.stdout.readline().strip() == "a tapped"
    t0 = time.monotonic()
    b = _script(srv, f"""
        p = Phone(port={srv.port})
        print(p.use()); p.read_text(); p.tap(20, 20); print("b tapped")
    """)
    out, _ = b.communicate(timeout=20)
    waited = time.monotonic() - t0
    a.wait(10)
    assert "in use by python" in out and "(pid " in out, out
    assert "b tapped" in out
    assert waited >= IDLE_S  # b waited for a's idle time instead of tapping in between


def test_input_after_losing_the_phone_is_not_sent(srv):
    a, b = _phone(srv, "agent-a"), _phone(srv, "agent-b")
    a.read_text()
    a.tap(100, 100)
    b.read_text()  # waits for a's idle time, then takes the phone
    b.tap(200, 200)
    lines = srv.inputs.read_text().splitlines()
    with pytest.raises(ScreenChanged, match="agent-b used the phone"):
        a.tap(100, 100)  # a's coordinates are from before b's turn
    assert srv.inputs.read_text().splitlines() == lines  # nothing reached the phone
    a.read_text()  # a looks again
    a.tap(100, 100)
    assert len(srv.inputs.read_text().splitlines()) == len(lines) + 1


def test_inputs_that_need_no_old_coordinates_are_sent_after_losing_the_phone(srv):
    a, b = _phone(srv, "agent-c"), _phone(srv, "agent-d")
    a.read_text()
    b.read_text()
    sent = len(srv.inputs.read_text().splitlines())
    a.launch_app("com.apple.Preferences")  # needs no target on the screen: sent
    a.press_button("home")
    assert len(srv.inputs.read_text().splitlines()) == sent + 2
    with pytest.raises(ScreenChanged):  # but a has not seen where they left the screen
        a.tap(3, 3)
    a.read_text()
    a.tap(3, 3)


def test_busy_when_the_person_holds_the_phone(srv):
    a = _phone(srv, "agent-e")
    a._admin("POST", "hold", {"seconds": 30})
    try:
        t0 = time.monotonic()
        with pytest.raises(Busy, match="person using it"):
            a.tap(5, 5)
        assert WAIT_S <= time.monotonic() - t0 < WAIT_S + 2
    finally:
        a._admin("POST", "hold", {"seconds": 0})


def test_release_hands_the_phone_over_at_once(srv):
    with _phone(srv, "agent-f") as a:
        a.read_text()
    b = _phone(srv, "agent-g")
    t0 = time.monotonic()
    b.read_text()
    assert time.monotonic() - t0 < IDLE_S / 2


def test_status_tells_who_has_the_phone(srv):
    a, b = _phone(srv, "agent-h"), _phone(srv, "agent-i")
    a.read_text()
    assert "use: in use by you" in a._text("status")
    assert "use: in use by agent-h" in b._text("status")


def test_the_log_names_the_client_of_each_call(srv):
    _phone(srv, "agent-j").read_text()
    assert "agent-j: read_text" in srv.log.read_text()


def test_a_script_started_by_a_client_counts_as_that_client(srv):
    # An agent that runs a flow script (or a screenshot helper) is acting itself: the script
    # must not wait for the agent's idle time, and the agent's next tap is not stale after it.
    child = (f"from phonectl.client import Phone; import time; t = time.monotonic(); c = Phone(port={srv.port}); "
             f"c.tap(2, 2); print('child waited', round(time.monotonic() - t, 2)); print('released', c.release())")
    parent = _script(srv, f"""
        import subprocess, sys
        p = Phone(port={srv.port})
        p.read_text(); p.tap(1, 1)
        r = subprocess.run([sys.executable, "-c", {child!r}], capture_output=True, text=True)
        print(r.stdout.strip(), r.stderr.strip()[-300:])
        p.tap(3, 3); print("parent tapped again")
    """)
    out, _ = parent.communicate(timeout=30)
    assert "child waited" in out, out
    waited = float(out.split("child waited ")[1].split()[0])
    assert waited < IDLE_S / 2, out
    assert "parent tapped again" in out, out
    assert "released False" in out, out  # the script ending does not give up the parent's phone


def test_a_new_client_looks_before_its_first_blind_input(srv):
    # The server cannot know who used the phone before it started, or before a client was
    # forgotten: a client it has not seen look at the screen gets NOT DONE for a blind tap.
    a = _phone(srv, "agent-k")
    with pytest.raises(ScreenChanged):
        a.tap(1, 1)
    with pytest.raises(ScreenChanged):
        a.tap(1, 1)  # asked for no screenshot, so it still has not seen the screen
    a.read_text()
    a.tap(1, 1)


def test_the_phone_is_free_as_soon_as_its_holder_exits(srv):
    a = _script(srv, f"""
        p = Phone(port={srv.port})
        p.read_text(); print("a looked", flush=True)
    """)
    a.communicate(timeout=20)  # a exits without releasing
    b = _phone(srv, "agent-l")
    t0 = time.monotonic()
    b.read_text()
    assert time.monotonic() - t0 < IDLE_S / 2
