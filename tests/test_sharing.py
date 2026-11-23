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

from http_client import Phone, PhoneError, ScreenChanged, Waiting

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
           "PHONECTL_IDLE": str(IDLE_S), "PHONECTL_WAIT": str(WAIT_S), "PHONECTL_LOG": "INFO",
           "PYTHONPATH": str(HERE)}  # client scripts in other processes import http_client
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
    code = "from http_client import *\nimport time\n" + textwrap.dedent(body)
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
    assert f"(pid {a.pid})" in out, out  # names a's process (its name is Python on some installs)
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
    b.tap(4, 4)  # b changes the screen
    sent = len(srv.inputs.read_text().splitlines())
    a.launch_app("com.apple.Preferences")  # needs no target on the screen: sent
    a.press_button("home")
    assert len(srv.inputs.read_text().splitlines()) == sent + 2
    with pytest.raises(ScreenChanged):  # but a has not seen where they left the screen
        a.tap(3, 3)
    a.read_text()
    a.tap(3, 3)


def test_waiting_when_the_person_holds_the_phone(srv):
    a = _phone(srv, "agent-e")
    a._admin("POST", "hold", {"seconds": 30})
    try:
        t0 = time.monotonic()
        with pytest.raises(Waiting, match="nothing was done: the person using the phone holds it") as w:
            a.tap(5, 5)
        assert WAIT_S <= time.monotonic() - t0 < WAIT_S + 2
        assert "call tap again now with the same arguments" in str(w.value)
    finally:
        a._admin("POST", "hold", {"seconds": 0})
    a.read_text()  # calls again, as told: its kept place is used, and holds up no later test


def test_a_call_its_client_cancelled_while_waiting_is_never_sent(srv):
    # A client on the 2025 wire that times out posts notifications/cancelled and stops waiting;
    # the input must not reach the phone when the line moves on later.
    import threading

    a, a_again = _phone(srv, "agent-m"), _phone(srv, "agent-m")
    a._admin("POST", "hold", {"seconds": 30})
    sent = len(srv.inputs.read_text().splitlines())
    outcome = []
    t = threading.Thread(target=lambda: outcome.append(_try(a.launch_app, "com.example.ghost")))
    t.start()
    time.sleep(0.5)
    assert a_again.cancel(a._id) == 202  # the id of the call now waiting
    t.join(10)
    a._admin("POST", "hold", {"seconds": 0})
    time.sleep(0.5)
    assert outcome and isinstance(outcome[0], PhoneError) and "Abandoned" in str(outcome[0]), outcome
    assert len(srv.inputs.read_text().splitlines()) == sent  # nothing reached the phone


def _try(fn, *args):
    try:
        return fn(*args)
    except PhoneError as e:
        return e


def test_a_script_yields_between_units_to_a_waiting_client(srv):
    a = _phone(srv, "agent-n")
    a.read_text()
    a.tap(1, 1)  # a has changed the screen: it holds the phone until it pauses or yields
    assert a._admin("POST", "yield") == {"yielded": False}  # nobody waits
    b = _script(srv, f"""
        p = Phone(name="agent-o", port={srv.port}); t = time.monotonic()
        p.read_text(); print("b waited", round(time.monotonic() - t, 2))
    """)
    time.sleep(0.5)
    assert a._admin("POST", "yield") == {"yielded": True}
    out, _ = b.communicate(timeout=10)
    assert float(out.split("b waited ")[1]) < IDLE_S, out


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
    a.tap(1, 1)
    assert "use: in use by you" in a._text("status")
    assert "use: in use by agent-h" in b._text("status")


def test_the_log_names_the_client_of_each_call(srv):
    _phone(srv, "agent-j").read_text()
    assert "agent-j: read_text" in srv.log.read_text()


def test_a_script_started_by_a_client_counts_as_that_client(srv):
    # An agent that runs a flow script (or a screenshot helper) is acting itself: the script
    # must not wait for the agent's idle time, and the agent's next tap is not stale after it.
    child = (f"from http_client import Phone; import time; t = time.monotonic(); c = Phone(port={srv.port}); "
             f"c.read_text(); c.tap(2, 2); print('child waited', round(time.monotonic() - t, 2)); "
             f"print('released', c.release())")  # its own process: it looks before its first tap
    parent = _script(srv, f"""
        import subprocess, sys
        p = Phone(port={srv.port})
        p.read_text(); p.tap(1, 1)
        r = subprocess.run([sys.executable, "-c", {child!r}], capture_output=True, text=True)
        print(r.stdout.strip(), r.stderr.strip()[-300:])
        try:
            p.tap(3, 3)
        except ScreenChanged as e:  # the script tapped: what the parent saw is out of date
            print("parent told to look:", e)
        p.read_text(); p.tap(3, 3); print("parent tapped again")
    """)
    out, _ = parent.communicate(timeout=30)
    assert "child waited" in out, out
    waited = float(out.split("child waited ")[1].split()[0])
    assert waited < IDLE_S / 2, out
    assert "parent tapped again" in out, out
    assert "released False" in out, out  # the script ending does not give up the parent's phone
    assert "parent told to look:" in out and "another process of yours" in out, out


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


def test_a_script_on_the_official_mcp_client_sees_refusals_as_errors(srv):
    # A script that knows nothing of phonectl, on the MCP SDK's own client: the input the server
    # did not send comes back as an error result, so the script cannot take it for done.
    import asyncio

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async def main():
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {srv.token}",
                                               "X-Phonectl-Client": "sdk-script"})
        async with streamable_http_client(f"http://127.0.0.1:{srv.port}/mcp", http_client=http) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                first = await s.call_tool("tap", {"x": 1, "y": 1, "screenshot": True})
                again = await s.call_tool("tap", {"x": 1, "y": 1, "screenshot": False})
                await s.call_tool("read_text", {})
                after_look = await s.call_tool("tap", {"x": 1, "y": 1, "screenshot": False})
                return first, again, after_look

    first, again, after_look = asyncio.run(main())
    assert first.is_error and first.content[0].text.startswith("NOT DONE:")
    # No screenshot in the refusal, and it is not a look: the next blind tap is refused too.
    assert [c.type for c in first.content] == ["text"]
    assert again.is_error
    assert not after_look.is_error


def test_text_taps_need_a_look_or_the_clients_own_app_after_someone_changed_the_screen(srv):
    from http_client import TextTimeout

    a, b = _phone(srv, "agent-p"), _phone(srv, "agent-q")
    a.read_text()
    b.read_text()
    b.tap(1, 1)  # b changes the screen
    with pytest.raises(ScreenChanged):
        a.tap_text("OK")  # "OK" may now be a button in b's app
    a.launch_app("com.example.mine")  # a plain launch shows the app as it was: maybe b's page
    with pytest.raises(ScreenChanged):
        a.tap_text("OK")
    a.launch_app("com.example.mine", restart=True)  # restarted: what is in front is a's
    with pytest.raises(TextTimeout):
        a.tap_text("OK")  # let through: the fake screen just has no text


def test_an_agents_end_of_turn_hook_gives_its_phone_up(srv):
    hook = (f"from http_client import Phone; print('hook', Phone(port={srv.port})._admin('POST', 'done'))")
    agent = _script(srv, f"""
        import subprocess, sys
        p = Phone(port={srv.port})
        p.read_text(); p.tap(1, 1)
        r = subprocess.run([sys.executable, "-c", {hook!r}], capture_output=True, text=True)
        print(r.stdout.strip(), r.stderr.strip()[-300:], flush=True)
        time.sleep(3)  # the agent goes on running: only the hook frees the phone
    """)
    line = agent.stdout.readline()
    assert "{'released': True}" in line, line
    b = _phone(srv, "agent-r")
    t0 = time.monotonic()
    b.read_text()
    assert time.monotonic() - t0 < IDLE_S / 2
    agent.wait(10)


def test_a_named_client_frees_the_phone_as_soon_as_it_exits(srv):
    a = _script(srv, f"""
        p = Phone(name="named-script", port={srv.port})
        p.read_text(); p.tap(1, 1)
    """)
    a.communicate(timeout=20)
    b = _phone(srv, "agent-s")
    t0 = time.monotonic()
    b.read_text()
    assert time.monotonic() - t0 < IDLE_S / 2
