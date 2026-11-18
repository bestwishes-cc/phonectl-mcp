import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from phonectl import server


def _call(name, **args):
    return asyncio.run(server.mcp.call_tool(name, args))


def _text(result):
    """The text a tool returned (call_tool gives a CallToolResult)."""
    return "".join(c.text for c in result.content if c.type == "text")


@pytest.fixture
def screen(monkeypatch):
    """A fake screen: tests set screen.looks to the OCR results of successive looks."""

    class Screen:
        looks: list[list[dict]] = []
        taps: list[tuple[float, float]] = []
        n = 0

    async def read_lines(find=""):
        look = Screen.looks[min(Screen.n, len(Screen.looks) - 1)]
        Screen.n += 1
        return [dict(l) for l in look if find in l["t"]]

    async def do_tap(x, y, hold_ms):
        Screen.taps.append((x, y))

    Screen.taps = []
    monkeypatch.setattr(server, "_read_lines", read_lines)
    monkeypatch.setattr(server, "_do_tap", do_tap)
    monkeypatch.setattr(server, "POLL_S", 0)
    return Screen


def _line(t, x, y):
    return {"t": t, "x": x, "y": y, "w": 40, "h": 20}


def test_tap_text_prefers_the_exact_line(screen):
    screen.looks = [[_line("登录后可查看", 100, 300), _line("登录", 200, 600)]]
    _call("tap_text", text="登录", screenshot=False)
    assert screen.taps == [(200, 600)]


def test_tap_text_refuses_to_guess_between_matches(screen):
    screen.looks = [[_line("关注", 350, 200), _line("关注", 350, 400)]]
    with pytest.raises(ToolError, match=r"2 times.*\(350, 200\).*\(350, 400\)"):
        _call("tap_text", text="关注", screenshot=False)
    assert screen.taps == []


def test_tap_text_waits_for_the_text(screen):
    screen.looks = [[], [], [_line("确定", 300, 700)]]
    _call("tap_text", text="确定", timeout_s=5, screenshot=False)
    assert screen.taps == [(300, 700)]


def test_tap_text_without_timeout_looks_once(screen):
    screen.looks = [[], [_line("确定", 300, 700)]]
    with pytest.raises(ToolError, match="TimeoutError: '确定' not on screen"):
        _call("tap_text", text="确定", screenshot=False)
    assert screen.n == 1


def test_wait_text_returns_the_lines(screen):
    screen.looks = [[], [_line("Settings", 60, 90), _line("Other", 10, 10)]]
    lines = json.loads(_text(_call("wait_text", text="Sett", timeout_s=5)))
    assert lines == [_line("Settings", 60, 90)]


def test_wait_text_gone(screen):
    screen.looks = [[_line("加载中", 200, 400)], []]
    assert _text(_call("wait_text", text="加载中", timeout_s=5, gone=True)) == "[]"


def test_wait_text_times_out(screen):
    screen.looks = [[_line("加载中", 200, 400)]]
    with pytest.raises(ToolError, match="still on screen after 0.05 s"):
        _call("wait_text", text="加载中", timeout_s=0.05, gone=True)


@pytest.fixture
def keyboard(monkeypatch):
    sent = []

    async def type_ascii(text):
        sent.append(("keys", text))

    async def set_clipboard(text):
        sent.append(("clipboard", text))

    async def do_key(spec):
        sent.append(("key", spec))

    monkeypatch.setattr(server.device, "type_ascii", type_ascii)
    monkeypatch.setattr(server.device, "set_clipboard", set_clipboard)
    monkeypatch.setattr(server, "_do_key", do_key)
    return sent


def test_type_text_pastes_any_text_as_is(keyboard):
    _call("type_text", text="a=1\nb", screenshot=False)
    _call("type_text", text="你好\n世界", screenshot=False)
    assert keyboard == [("clipboard", "a=1\nb"), ("key", "cmd+v"), ("clipboard", "你好\n世界"), ("key", "cmd+v")]


def test_type_text_keys_on_request(keyboard):
    _call("type_text", text="secret\n", method="keys", screenshot=False)
    assert keyboard == [("keys", "secret\n")]


def test_type_text_rejects_an_unknown_method(keyboard):
    with pytest.raises(ToolError):
        _call("type_text", text="hi", method="fast", screenshot=False)
    assert keyboard == []


def test_settle_ms_sets_the_wait(monkeypatch, keyboard):
    waits = []

    async def sleep(s):
        waits.append(s)

    async def capture():
        return "shot"

    monkeypatch.setattr(server.asyncio, "sleep", sleep)
    monkeypatch.setattr(server, "_capture", capture)
    assert asyncio.run(server._after("x", True, None)) == ["x", "shot"]
    asyncio.run(server._after("x", True, 150))
    assert asyncio.run(server._after("x", False, None)) == ["x"]
    assert asyncio.run(server._after("x", False, 300)) == ["x"]
    assert waits == [server.SETTLE_S, 0.15, 0.3]
