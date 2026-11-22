"""`phonectl setup <agent>`: connect one agent to the server and give it the phonectl-scripts skill.

Each agent keeps its MCP servers and its skills in its own place, in its own format; this module
knows those places for the agents below and writes them the agent's way (its own command where
it has one). Every file it changes is backed up as <file>.bak first. What it installed is
recorded in STATE, so `phonectl update` can bring the skill up to date after an upgrade, and a
skill the user edited since is left as it is.
"""

from __future__ import annotations

import hashlib
import json
import re
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


def _backup(path: Path, changed: list[str]) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
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
_TABLE = re.compile(rf"^\[mcp_servers\.{NAME}(\..*)?\]\s*$")


def _codex_without_phone(text: str) -> str:
    """config.toml with the [mcp_servers.phone] table (and its subtables) taken out."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            skipping = bool(_TABLE.match(line.strip()))
        if not skipping:
            out.append(line)
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


def _dsh_entry(url: str, token: str) -> list[str]:
    return ["      # phonectl setup: the shared phone server.\n",
            f"      {NAME}:\n",
            "        transport: streamable-http\n",
            f"        url: {url}\n",
            "        headers:\n",
            f"          Authorization: {json.dumps('Bearer ' + token)}\n"]


def _dsh_split(lines: list[str]) -> tuple[int, int, int]:
    """(start of the mcp-manager item, its servers: line, end of the item), -1 where absent."""
    start = next((i for i, l in enumerate(lines) if re.match(r"^- id:\s*mcp-manager\s*$", l)), -1)
    if start < 0:
        return -1, -1, len(lines)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("- ")), len(lines))
    servers = next((i for i in range(start, end) if re.match(r"^    servers:\s*$", lines[i])), -1)
    return start, servers, end


def _dsh_without_phone(lines: list[str]) -> list[str]:
    start, servers, end = _dsh_split(lines)
    if servers < 0:
        return lines
    out, i = lines[:servers + 1], servers + 1
    while i < end:
        line = lines[i]
        if re.match(rf"^      {NAME}:\s*$", line):
            while out and out[-1].strip().startswith("#") and out[-1].startswith("      #"):
                out.pop()  # the comment that belongs to the entry
            i += 1
            while i < end and (lines[i].startswith("        ") or not lines[i].strip()):
                i += 1
            continue
        out.append(line)
        i += 1
    return out + lines[end:]


def _dsh_write(lines: list[str], changed: list[str]) -> None:
    _backup(DSH_PATCH, changed)
    DSH_PATCH.write_text("".join(lines))


def _dsh_add(url: str, token: str, changed: list[str]) -> None:
    if not DSH_PATCH.exists():
        raise SetupError(f"{DSH_PATCH} not found: start DeepSeek Harness once first")
    lines = _dsh_without_phone(DSH_PATCH.read_text().splitlines(keepends=True))
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start, servers, end = _dsh_split(lines)
    entry = _dsh_entry(url, token)
    if start < 0:
        lines += ["- id: mcp-manager\n", "  config:\n", "    servers:\n", *entry]
    elif servers < 0:
        config = next((i for i in range(start, end) if re.match(r"^  config:\s*$", lines[i])), -1)
        if config < 0:
            lines[start + 1:start + 1] = ["  config:\n", "    servers:\n", *entry]
        else:
            lines[config + 1:config + 1] = ["    servers:\n", *entry]
    else:
        lines[servers + 1:servers + 1] = entry
    _dsh_write(lines, changed)


def _dsh_remove(changed: list[str]) -> None:
    if DSH_PATCH.exists():
        _dsh_write(_dsh_without_phone(DSH_PATCH.read_text().splitlines(keepends=True)), changed)


AGENTS = {a.name: a for a in [
    Agent("claude-code", "Claude Code", HOME / ".claude" / "skills",
          lambda: _which("claude") is not None or (HOME / ".claude.json").exists(),
          _claude_add, _claude_remove, "new sessions see it; in a running one, run /mcp"),
    Agent("codex", "Codex", HOME / ".codex" / "skills",
          lambda: (HOME / ".codex").exists() or _which("codex") is not None,
          _codex_add, _codex_remove, "restart Codex (or the ChatGPT app)"),
    Agent("gemini", "Gemini (desktop app)", HOME / ".gemini" / "config" / "skills",
          lambda: _which("agy", AGY) is not None,
          _agy_add, _agy_remove, "start a new Gemini conversation"),
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
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


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


def install(name: str, url: str, token: str, force: bool = False) -> list[str]:
    """Connect agent `name` and give it the skill. Lines to print."""
    agent = AGENTS[name]
    state = _state()
    record = state.get(name, {})
    changed: list[str] = []
    agent.add(url, token, changed)
    skill = _put_skill(agent, record, force)
    if "left as it is" not in skill:
        record = {"version": __version__, "skill_hash": _tree_hash(agent.skills / SKILL)}
    state[name] = {**record, "mcp_url": url}
    _save_state(state)
    return [f"{agent.title}: MCP server '{NAME}' -> {url}; {skill}",
            *(f"  changed {c} (backup: {c}.bak)" for c in changed),
            f"  to use it: {agent.reload}"]


def uninstall(name: str) -> list[str]:
    agent = AGENTS[name]
    state = _state()
    record = state.pop(name, {})
    changed: list[str] = []
    agent.remove(changed)
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
            *(f"  changed {c} (backup: {c}.bak)" for c in changed),
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
