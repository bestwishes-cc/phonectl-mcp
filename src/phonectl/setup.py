"""`phonectl setup <agent>`: connect one agent to the server and give it the phonectl-scripts skill.

Each agent keeps its MCP servers and its skills in its own place, in its own format; this module
knows those places for the agents below and writes them the agent's way (its own command where
it has one). The first time it changes a file, it keeps the original as <file>.phonectl.bak. What it installed is
recorded in STATE, so `phonectl update` can bring the skill up to date after an upgrade, and a
skill the user edited since is left as it is.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import __version__

SKILL = "phonectl-scripts"
SKILL_SRC = Path(__file__).parent / "skills" / SKILL
HOME = Path.home()
STATE = HOME / ".config" / "phonectl" / "setup.json"
# Earlier copies of a replaced skill go here, not next to it: an agent would load a copy next
# to it as a second skill.
SKILL_BACKUPS = HOME / ".config" / "phonectl" / "skill-backups"
NAME = "phone"  # the MCP server's name in every agent


class SetupError(RuntimeError):
    pass


def _run(*cmd: str) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SetupError(f"`{' '.join(cmd[:3])} ...` failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


BACKUP = ".phonectl.bak"  # its own suffix: a <file>.bak the user or another tool made is not ours


def _backup(path: Path, changed: list[str]) -> None:
    """Keep the file as it was before phonectl first changed it, as <file>.phonectl.bak. A later
    run does not overwrite that copy: it would replace the user's original with phonectl's edit."""
    bak = path.with_name(path.name + BACKUP)
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)
    changed.append(str(path))


# --- the agents -----------------------------------------------------------------------------

@dataclass
class Agent:
    name: str  # as typed after `phonectl setup`
    title: str
    skills: Path  # the user-level skills folder it reads
    found: Callable[[], bool]
    add: Callable[[str, str, list[str]], None]  # (url, token, changed files)
    remove: Callable[[list[str]], None]
    reload: str  # what the user does for the agent to see the change
    # Its end-of-turn hook (see _hook_command): (command, changed files) to put it in, and
    # (changed files) to take it out. None where the agent has no hooks phonectl can set.
    add_hook: Optional[Callable[[str, list[str]], None]] = None
    remove_hook: Optional[Callable[[list[str]], None]] = None


def _which(*candidates: str) -> Optional[str]:
    for c in candidates:
        if "/" in c:
            if Path(c).exists():
                return c
        elif (p := shutil.which(c)):
            return p
    return None


# Claude Code: its own command, user scope (~/.claude.json).

def _claude() -> str:
    exe = _which("claude", str(HOME / ".claude/local/claude"), "/opt/homebrew/bin/claude")
    if exe is None:
        raise SetupError("the `claude` command was not found")
    return exe


def _claude_add(url: str, token: str, changed: list[str]) -> None:
    _backup(HOME / ".claude.json", changed)
    subprocess.run([_claude(), "mcp", "remove", NAME, "--scope", "user"], capture_output=True)
    _run(_claude(), "mcp", "add", "--scope", "user", "--transport", "http", NAME, url,
         "--header", f"Authorization: Bearer {token}")


def _claude_remove(changed: list[str]) -> None:
    _backup(HOME / ".claude.json", changed)
    subprocess.run([_claude(), "mcp", "remove", NAME, "--scope", "user"], capture_output=True)


# Codex: ~/.codex/config.toml. `codex mcp add` takes a token only from an environment variable,
# which an app started from the Dock does not have, so the table is written here.

CODEX_TOML = HOME / ".codex" / "config.toml"
_TABLE = re.compile(rf"^\[mcp_servers\.{NAME}(\.[^\]]*)?\]\s*(#.*)?$")


def _codex_without_phone(text: str) -> str:
    """config.toml with the [mcp_servers.phone] table (and its subtables) taken out. Comments
    just above the next table belong to that table and stay."""
    out, skipped, skipping = [], [], False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            if skipping:  # give the comments right above this header back to it
                tail = len(skipped)
                while tail and (not skipped[tail - 1].strip() or skipped[tail - 1].lstrip().startswith("#")):
                    tail -= 1
                out += skipped[tail:]
                skipped = []
            skipping = bool(_TABLE.match(line.strip()))
        (skipped if skipping else out).append(line)
    if skipping:  # the table ran to the end: comments after its last value are not part of it
        tail = len(skipped)
        while tail and (not skipped[tail - 1].strip() or skipped[tail - 1].lstrip().startswith("#")):
            tail -= 1
        out += skipped[tail:]
    return "".join(out).rstrip("\n") + ("\n" if out else "")


def _codex_write(text: str, changed: list[str]) -> None:
    tomllib.loads(text)  # never leave Codex a config it cannot read
    _backup(CODEX_TOML, changed)
    CODEX_TOML.parent.mkdir(parents=True, exist_ok=True)
    CODEX_TOML.write_text(text)


def _codex_add(url: str, token: str, changed: list[str]) -> None:
    text = _codex_without_phone(CODEX_TOML.read_text() if CODEX_TOML.exists() else "")
    table = (f'\n[mcp_servers.{NAME}]\nurl = {json.dumps(url)}\n'
             f'http_headers = {{ Authorization = {json.dumps("Bearer " + token)} }}\n')
    _codex_write(text + table, changed)


def _codex_remove(changed: list[str]) -> None:
    if CODEX_TOML.exists():
        _codex_write(_codex_without_phone(CODEX_TOML.read_text()), changed)


# Gemini (desktop app's agent, agy): its own command (~/.gemini/config/mcp_config.json).

AGY = "/Applications/Gemini.app/Contents/Helpers/agy"


def _agy() -> str:
    exe = _which("agy", AGY)
    if exe is None:
        raise SetupError("Gemini's `agy` was not found")
    return exe


def _agy_add(url: str, token: str, changed: list[str]) -> None:
    _backup(HOME / ".gemini" / "config" / "mcp_config.json", changed)
    _run(_agy(), "mcp", "add", "--header", f"Authorization: Bearer {token}", NAME, url)
    subprocess.run([_agy(), "mcp", "enable", NAME], capture_output=True)  # if it was disabled


def _agy_remove(changed: list[str]) -> None:
    _backup(HOME / ".gemini" / "config" / "mcp_config.json", changed)
    subprocess.run([_agy(), "mcp", "remove", NAME], capture_output=True)


# DeepSeek Harness: the desktop profile's patch file, under the mcp-manager entry. It has no
# command for this; it reloads the file when it changes.

DSH_PATCH = HOME / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
_MARK = "# phonectl setup: the shared phone server."


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    """Not blank and not a comment."""
    return bool(line.strip()) and not line.lstrip().startswith("#")


@dataclass
class _DshLayout:
    """Where the phone entry goes in the patch file; -1 where a part is missing."""
    item: int = -1  # the `- id: mcp-manager` line
    end: int = 0  # the line after the item
    config: int = -1
    sub: int = -1  # indentation of the keys under config
    servers: int = -1
    block_end: int = -1  # the line after the servers block
    child: int = -1  # indentation of the entries under servers
    phone: int = -1  # the `phone:` line
    phone_end: int = -1  # the line after the phone entry


def _dsh_layout(lines: list[str]) -> _DshLayout:
    """Read the parts of the file this module edits. Anything written in a way it does not
    expect (a flow mapping, an inline value, a list under servers, an mcp-manager item that is
    not a top-level `- id: mcp-manager`, two of them) raises SetupError: an edit by guesswork
    could merge the user's servers into ours, drop them, or leave YAML the harness cannot read."""
    lay = _DshLayout(end=len(lines))
    for i, line in enumerate(lines):
        if re.search(r"\bid:\s*mcp-manager\b", line) and not re.match(r"^- id:\s*mcp-manager\s*(#.*)?$", line):
            raise SetupError(f"{DSH_PATCH} line {i + 1}: the mcp-manager entry is written in a form "
                             "phonectl does not edit; add the phone server by hand")
    items = [i for i, l in enumerate(lines) if re.match(r"^- id:\s*mcp-manager\s*(#.*)?$", l)]
    if len(items) > 1:
        raise SetupError(f"{DSH_PATCH} has {len(items)} mcp-manager entries; merge them by hand first")
    if not items:
        return lay
    lay.item = items[0]
    lay.end = next((i for i in range(lay.item + 1, len(lines)) if _content(lines[i]) and _indent(lines[i]) == 0),
                   len(lines))

    def block(start: int, indent: int) -> int:
        """The line after the block of lines indented deeper than indent, from start on."""
        return next((i for i in range(start, lay.end) if _content(lines[i]) and _indent(lines[i]) <= indent), lay.end)

    def key(name: str, start: int, stop: int, indent: int) -> int:
        """The `name:` key directly at this indentation (not one nested deeper), -1 if absent."""
        for i in range(start, stop):
            if _indent(lines[i]) == indent and re.match(rf"^\s+{name}:", lines[i]):
                if not re.match(rf"^\s+{name}:\s*(#.*)?$", lines[i]):
                    raise SetupError(f"{DSH_PATCH} line {i + 1}: `{name}:` has an inline value; "
                                     "phonectl only edits the block form")
                return i
        return -1

    lay.config = key("config", lay.item + 1, lay.end, 2)  # the item's keys line up with `id`
    if lay.config < 0:
        return lay
    top = _indent(lines[lay.config])
    config_end = block(lay.config + 1, top)
    keys = [i for i in range(lay.config + 1, config_end) if _content(lines[i])]
    lay.sub = _indent(lines[keys[0]]) if keys else top + 2
    lay.servers = key("servers", lay.config + 1, config_end, lay.sub)
    if lay.servers < 0:
        return lay
    top = _indent(lines[lay.servers])
    lay.block_end = block(lay.servers + 1, top)
    entries = [i for i in range(lay.servers + 1, lay.block_end) if _content(lines[i])]
    if entries and lines[entries[0]].lstrip().startswith("-"):
        raise SetupError(f"{DSH_PATCH} line {entries[0] + 1}: servers is a list here, not a mapping; "
                         "add the phone server by hand")
    lay.child = _indent(lines[entries[0]]) if entries else top + 2
    for i in entries:
        if _indent(lines[i]) == lay.child and re.match(rf"^\s+{NAME}:", lines[i]):
            if not re.match(rf"^\s+{NAME}:\s*(#.*)?$", lines[i]):
                raise SetupError(f"{DSH_PATCH} line {i + 1}: `{NAME}:` has an inline value; edit it by hand")
            lay.phone = i
            lay.phone_end = next((j for j in range(i + 1, lay.block_end)
                                  if _content(lines[j]) and _indent(lines[j]) <= lay.child), lay.block_end)
            # Blank lines after the entry, and comments no deeper than the entries, belong to
            # what follows (a note above the next server, a server commented out), not to it.
            while lay.phone_end > i + 1 and (not lines[lay.phone_end - 1].strip() or (
                    lines[lay.phone_end - 1].lstrip().startswith("#")
                    and _indent(lines[lay.phone_end - 1]) <= lay.child)):
                lay.phone_end -= 1
    return lay


def _dsh_entry(indent: int, url: str, token: str) -> list[str]:
    pad = " " * indent
    return [f"{pad}{NAME}:\n", f"{pad}  transport: streamable-http\n", f"{pad}  url: {url}\n",
            f"{pad}  headers:\n", f"{pad}    Authorization: {json.dumps('Bearer ' + token)}\n"]


def _dsh_read() -> list[str]:
    lines = DSH_PATCH.read_text().splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def _dsh_write(lines: list[str], changed: list[str]) -> None:
    lay = _dsh_layout(lines)  # never leave a file this module could not read back
    if lay.phone >= 0 and sum(1 for l in lines[lay.servers + 1:lay.block_end]
                              if _indent(l) == lay.child and re.match(rf"^\s+{NAME}:", l)) != 1:
        raise SetupError(f"{DSH_PATCH}: the edit would leave more than one `{NAME}` entry; nothing written")
    _backup(DSH_PATCH, changed)
    DSH_PATCH.write_text("".join(lines))


def _dsh_add(url: str, token: str, changed: list[str]) -> None:
    if not DSH_PATCH.exists():
        raise SetupError(f"{DSH_PATCH} not found: start DeepSeek Harness once first")
    lines = _dsh_read()
    lay = _dsh_layout(lines)
    if lay.item < 0:
        lines += ["- id: mcp-manager\n", "  config:\n", "    servers:\n", f"      {_MARK}\n",
                  *_dsh_entry(6, url, token)]
    elif lay.config < 0:
        lines[lay.item + 1:lay.item + 1] = ["  config:\n", "    servers:\n", f"      {_MARK}\n",
                                            *_dsh_entry(6, url, token)]
    elif lay.servers < 0:
        ind = lay.sub
        lines[lay.config + 1:lay.config + 1] = [" " * ind + "servers:\n", " " * (ind + 2) + _MARK + "\n",
                                                *_dsh_entry(ind + 2, url, token)]
    elif lay.phone >= 0:
        lines[lay.phone:lay.phone_end] = _dsh_entry(lay.child, url, token)  # in place, comments kept
    else:
        lines[lay.servers + 1:lay.servers + 1] = [" " * lay.child + _MARK + "\n",
                                                  *_dsh_entry(lay.child, url, token)]
    _dsh_write(lines, changed)


def _dsh_remove(changed: list[str]) -> None:
    if not DSH_PATCH.exists():
        return
    lines = _dsh_read()
    lay = _dsh_layout(lines)
    if lay.phone < 0:
        return
    start = lay.phone - 1 if lay.phone > 0 and lines[lay.phone - 1].strip() == _MARK else lay.phone
    del lines[start:lay.phone_end]
    _dsh_write(lines, changed)


# --- end-of-turn hooks ------------------------------------------------------------------
#
# When an agent's turn ends, it is done with the phone; without a hook the server only finds
# that out once the agent has paused for longer than its pace (see arbiter.grace). The hook
# runs a command that tells the server so (POST /phonectl/done), reading the token from its
# file so that no settings file holds it.

_DONE = "/phonectl/done"  # in every hook command phonectl writes: how its own hooks are found


def _hook_command(url: str, token_path: Path) -> str:
    """A sh command that never fails the agent's turn: it prints {} and exits 0 whatever happens
    (no curl, no server, a path with quotes in it)."""
    base = url.rsplit("/mcp", 1)[0]
    header = shlex.quote("Authorization: Bearer ") + f'"$(cat {shlex.quote(str(token_path))} 2>/dev/null)"'
    return f"curl -s -m 3 -o /dev/null -X POST -H {header} {shlex.quote(base + _DONE)} >/dev/null 2>&1; echo '{{}}'"


def _ours(handler: object) -> bool:
    return isinstance(handler, dict) and _DONE in str(handler.get("command", ""))


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise SetupError(f"{path} is not valid JSON ({e}); fix it first") from None
    if not isinstance(data, dict):
        raise SetupError(f"{path} is not a JSON object; fix it first")
    return data


def _write_json(path: Path, data: dict, changed: list[str]) -> None:
    _backup(path, changed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# Claude Code: Stop (its turn ended) in ~/.claude/settings.json, next to whatever hooks are
# there already. Not SubagentStop: a subagent that finishes (one that read code, say) does not
# mean another one, or the session, is done with the phone.
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
_CLAUDE_EVENTS = ("Stop",)
_CLAUDE_OLD_EVENTS = ("Stop", "SubagentStop")  # where earlier versions put it: taken out of both


def _claude_unhook(data: dict) -> dict:
    """data without phonectl's hooks; everything else, empty lists included, as it was."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data
    emptied = False
    for event in _CLAUDE_OLD_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept, removed = [], False
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list) and any(_ours(h) for h in g["hooks"]):
                removed = True
                g = {**g, "hooks": [h for h in g["hooks"] if not _ours(h)]}
                if not g["hooks"]:
                    continue
            kept.append(g)
        if removed and not kept:
            del hooks[event]
            emptied = True
        elif removed:
            hooks[event] = kept
    if emptied and not hooks:
        del data["hooks"]
    return data


def _claude_hook(command: str, changed: list[str]) -> None:
    data = _claude_unhook(_load_json(CLAUDE_SETTINGS))
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError(f"{CLAUDE_SETTINGS}: hooks is not an object; fix it first")
    for event in _CLAUDE_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SetupError(f"{CLAUDE_SETTINGS}: hooks.{event} is not a list; fix it first")
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
    _write_json(CLAUDE_SETTINGS, data, changed)


def _claude_unhook_file(changed: list[str]) -> None:
    if not CLAUDE_SETTINGS.exists():
        return
    data = _load_json(CLAUDE_SETTINGS)
    before = json.dumps(data)
    data = _claude_unhook(data)
    if json.dumps(data) != before:  # leave the file as it is when none of ours was in it
        _write_json(CLAUDE_SETTINGS, data, changed)


# Gemini (agy): a named hook in ~/.gemini/config/hooks.json; Stop fires when its turn ends.
AGY_HOOKS = HOME / ".gemini" / "config" / "hooks.json"
_AGY_HOOK_NAME = "phonectl-release"


def _agy_hook(command: str, changed: list[str]) -> None:
    data = _load_json(AGY_HOOKS)
    data[_AGY_HOOK_NAME] = {"Stop": [{"type": "command", "command": command, "timeout": 5}]}
    _write_json(AGY_HOOKS, data, changed)


def _agy_unhook(changed: list[str]) -> None:
    data = _load_json(AGY_HOOKS)
    if data.pop(_AGY_HOOK_NAME, None) is not None:
        _write_json(AGY_HOOKS, data, changed)


AGENTS = {a.name: a for a in [
    Agent("claude-code", "Claude Code", HOME / ".claude" / "skills",
          lambda: _which("claude") is not None or (HOME / ".claude.json").exists(),
          _claude_add, _claude_remove, "new sessions see it; in a running one, run /mcp",
          _claude_hook, _claude_unhook_file),
    Agent("codex", "Codex", HOME / ".codex" / "skills",
          lambda: (HOME / ".codex").exists() or _which("codex") is not None,
          _codex_add, _codex_remove, "restart Codex (or the ChatGPT app)"),
    Agent("gemini", "Gemini (desktop app)", HOME / ".gemini" / "config" / "skills",
          lambda: _which("agy", AGY) is not None,
          _agy_add, _agy_remove, "start a new Gemini conversation", _agy_hook, _agy_unhook),
    Agent("deepseek-harness", "DeepSeek Harness", HOME / ".dsh" / "skills",
          lambda: DSH_PATCH.exists(),
          _dsh_add, _dsh_remove, "nothing: it reloads its config and skills by itself"),
]}


# --- the skill and the record of what was installed -----------------------------------------

def _tree_hash(root: Path) -> Optional[str]:
    if not root.is_dir():
        return None
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def _state() -> dict:
    if not STATE.exists():
        return {}
    # Not {} on failure: saving over it would forget which skills phonectl installed, and every
    # one of them would then count as edited by hand and never be updated again.
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError) as e:
        raise SetupError(f"cannot read {STATE} ({e}); fix it or delete it, then run setup again") from None
    if not isinstance(state, dict) or not all(isinstance(v, dict) for v in state.values()):
        raise SetupError(f"{STATE} is not a record phonectl wrote; fix it or delete it, then run setup again")
    return state


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def _put_skill(agent: Agent, record: dict, force: bool) -> str:
    """Copy the skill in; what happened, in a few words."""
    dest = agent.skills / SKILL
    have, ours = _tree_hash(dest), _tree_hash(SKILL_SRC)
    if have == ours:
        return "skill up to date"
    if have is not None and have != record.get("skill_hash") and not force:
        return (f"skill left as it is: {dest} was changed by hand (or not installed by phonectl); "
                "--force replaces it")
    if have is not None:
        backup = SKILL_BACKUPS / agent.name / SKILL
        shutil.rmtree(backup, ignore_errors=True)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dest, backup)
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SKILL_SRC, dest)
    return f"skill installed in {dest}"


def _changed_lines(changed: list[str]) -> list[str]:
    return [f"  changed {c} (original kept as {c}{BACKUP})" if Path(c + BACKUP).exists()
            else f"  created {c}" for c in sorted(set(changed))]


def install(name: str, url: str, token: str, force: bool = False,
            token_path: Optional[Path] = None) -> list[str]:
    """Connect agent `name`, give it the skill and, where it has hooks, the end-of-turn hook.
    Lines to print."""
    agent = AGENTS[name]
    state = _state()
    record = state.get(name, {})
    changed: list[str] = []
    agent.add(url, token, changed)
    if agent.add_hook is not None and token_path is not None:
        agent.add_hook(_hook_command(url, token_path), changed)
        hook = "end-of-turn hook set"
    else:
        hook = "no end-of-turn hook (the server goes by its pace)"
    skill = _put_skill(agent, record, force)
    if "left as it is" not in skill:
        record = {"version": __version__, "skill_hash": _tree_hash(agent.skills / SKILL)}
    state[name] = {**record, "mcp_url": url}
    _save_state(state)
    return [f"{agent.title}: MCP server '{NAME}' -> {url}; {skill}; {hook}",
            *_changed_lines(changed),
            f"  to use it: {agent.reload}"]


def uninstall(name: str) -> list[str]:
    agent = AGENTS[name]
    state = _state()
    record = state.pop(name, {})
    changed: list[str] = []
    agent.remove(changed)
    if agent.remove_hook is not None:
        agent.remove_hook(changed)
    dest = agent.skills / SKILL
    if dest.is_dir() and _tree_hash(dest) == record.get("skill_hash"):
        shutil.rmtree(dest)
        skill = f"skill removed from {dest}"
    elif dest.is_dir():
        skill = f"skill left in {dest}: it was changed by hand"
    else:
        skill = "no skill to remove"
    _save_state(state)
    return [f"{agent.title}: MCP server '{NAME}' removed; {skill}",
            *_changed_lines(changed),
            f"  then: {agent.reload}"]


def installed() -> list[str]:
    return [n for n in _state() if n in AGENTS]


def stale() -> list[str]:
    """Agents whose phonectl-installed skill is older than this version's."""
    ours = _tree_hash(SKILL_SRC)
    out = []
    for name, record in _state().items():
        agent = AGENTS.get(name)
        if agent is None:
            continue
        have = _tree_hash(agent.skills / SKILL)
        if have is not None and have == record.get("skill_hash") and have != ours:
            out.append(agent.title)
    return out
