"""Who may use the phone right now, when several clients share one server.

The phone has one screen, so two agents driving it at once take turns call by call and act on
each other's screens. Here a client that starts using the phone holds it until it has made no
call for IDLE_S; a call from anyone else meanwhile waits in line (up to WAIT_S, then Busy)
instead of landing on the holder's screen. Waiting inside the call keeps the waiting agent's
turn alive: it carries on by itself once the phone is free, with no tokens spent while it waits.

The holder is never cut off while it keeps working, except that after MAX_HOLD_S it lets a
waiting client go first. A client whose idle time ran out and who then lost the phone to
someone else is told so on its next blind input (a tap at coordinates read before), which is
then not sent: see Lease.stale_by.

Clients are told apart without any token of their own: by the process at the other end of
the TCP connection (a Claude Code session and its subagents are one process; each script
run is one), or by the name in an X-Phonectl-Client header when one process runs several
independent tasks. A process started by a client counts as that client: a flow script or a
screenshot helper an agent runs is the agent's own action, and must neither wait for the
agent nor make its next tap stale. Locks used here are advisory between clients that use this server; the
person holding the phone is invisible to it, which is what `phonectl hold` is for.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

log = logging.getLogger(__name__)

# A holder that made no call for this long has finished with the phone. Long enough for an
# agent to think between two calls (DeepSeek at max reasoning takes over a minute), short
# enough that the next task does not sit idle for long after one ends.
IDLE_S = float(os.environ.get("PHONECTL_IDLE", "90"))
# How long a call waits for the phone before failing with Busy. Below the 300 s read timeout
# of the MCP Python client; a wait longer than a client's own timeout fails there first.
WAIT_S = float(os.environ.get("PHONECTL_WAIT", "180"))
# After holding this long without a break, the holder lets a waiting client go first.
MAX_HOLD_S = float(os.environ.get("PHONECTL_MAX_HOLD", "600"))
# Clients not seen for this long are forgotten.
FORGET_S = 3600.0
# The person using the phone by hand (phonectl hold).
PERSON = "person"
# While the holder's process is known, a waiting call checks this often whether it has exited.
_EXIT_POLL_S = 1.0


class Busy(RuntimeError):
    """The phone stayed in use by someone else for the whole wait."""


@dataclass
class Client:
    key: str  # what tells clients apart: "pid:123", "name:<header>", "person", "local"
    label: str  # shown to other clients and in the log
    seen: float = 0.0  # monotonic time of the last call
    pid: Optional[int] = None  # its process, when known: once that exits, it holds nothing
    # Label of whoever used the phone after this client last saw the screen; None if nobody.
    disturbed_by: Optional[str] = None
    disturbances: int = 0  # how many times disturbed_by was set


@dataclass
class Lease:
    """Handed to a call that got the phone."""

    client: Client
    # Set when someone else used the phone since this client last looked at the screen, so
    # coordinates it read before may point at something else now.
    stale_by: Optional[str]
    disturbances: int  # the client's count when the call started


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # someone else's process: it exists
        pass
    return True


class Arbiter:
    def __init__(self, idle_s: float = IDLE_S, wait_s: float = WAIT_S, max_hold_s: float = MAX_HOLD_S,
                 clock: Callable[[], float] = time.monotonic):
        self.idle_s, self.wait_s, self.max_hold_s = idle_s, wait_s, max_hold_s
        self._clock = clock
        self._cond = asyncio.Condition()
        self._clients: dict[str, Client] = {}
        self._holder: Optional[Client] = None
        self._held_since = 0.0
        self._last_end = 0.0  # when the holder's last call ended
        self._active = 0  # calls of the holder in progress
        self._queue: deque[Client] = deque()
        self._person_until = 0.0
        # Who used the phone last; a client seen for the first time has not looked since.
        self._last_user: Optional[str] = None

    def assume_used_before(self) -> None:
        """The phone may have been used before this server started (a restart): every client's
        first blind input has to be preceded by a look."""
        self._last_user = self._last_user or "someone before this server started"

    def _disturb(self, client: Client, by: str) -> None:
        client.disturbed_by = by
        client.disturbances += 1

    # --- identity -----------------------------------------------------------------------

    def known(self, key: str) -> Optional[Client]:
        return self._clients.get(key)

    def client(self, key: str, label: str, pid: Optional[int] = None) -> Client:
        """The record for this client, made on first sight."""
        now = self._clock()
        for k in [k for k, c in self._clients.items() if now - c.seen > FORGET_S and c is not self._holder]:
            del self._clients[k]
        c = self._clients.get(key)
        if c is None:
            c = self._clients[key] = Client(key, label, now, pid)
            # New, or forgotten after FORGET_S: it has not seen what the last user left.
            if self._last_user is not None:
                self._disturb(c, self._last_user)
        c.label = label
        return c

    # --- use ----------------------------------------------------------------------------

    @asynccontextmanager
    async def use(self, client: Client) -> AsyncIterator[Lease]:
        """Hold the phone for one call, waiting in line for it if someone else has it."""
        await self._acquire(client)
        lease = Lease(client, client.disturbed_by, client.disturbances)
        try:
            yield lease
        finally:
            async with self._cond:
                self._active -= 1
                client.seen = self._last_end = self._clock()
                self._cond.notify_all()

    def saw_screen(self, lease: Lease) -> None:
        """The call looked at the screen: the client's coordinates are current, unless it was
        disturbed again while the call ran (the person took the phone meanwhile)."""
        if lease.client.disturbances == lease.disturbances:
            lease.client.disturbed_by = None

    async def _acquire(self, client: Client) -> None:
        deadline = self._clock() + self.wait_s
        async with self._cond:
            if self._may_take(client, first_in_line=not self._queue):
                self._take(client)
                return
            self._queue.append(client)
            log.info("%s waits for the phone (held by %s)", client.label, self._holder_label())
            try:
                while True:
                    now = self._clock()
                    if self._may_take(client, first_in_line=self._queue[0] is client):
                        break
                    if now >= deadline:
                        raise Busy(self._busy_message(waited=self.wait_s))
                    # Wake for a release, a call ending, or the holder's idle time running out.
                    wake = min(deadline, self._free_at())
                    try:
                        async with asyncio.timeout(max(wake - now, 0.01)):
                            await self._cond.wait()
                    except TimeoutError:
                        pass
            finally:
                self._queue.remove(client)
                self._cond.notify_all()
            self._take(client)

    def _may_take(self, client: Client, first_in_line: bool) -> bool:
        now = self._clock()
        if now < self._person_until and client.key != PERSON:
            return False
        holder = self._holder
        if holder is None:
            return first_in_line
        if holder is client:
            # The holder goes on, unless it has had the phone for long and someone is waiting.
            return not self._queue or now - self._held_since < self.max_hold_s or first_in_line
        if self._active:
            return False
        idle = now - self._last_end
        done = idle >= self.idle_s or now - self._held_since >= self.max_hold_s or self._holder_exited()
        return first_in_line and done

    def _holder_exited(self) -> bool:
        """The holder's process is gone (a script or a one-shot agent run that ended)."""
        h = self._holder
        return h is not None and h.pid is not None and not _alive(h.pid)

    def _free_at(self) -> float:
        """Earliest time the phone may change hands without anyone releasing it."""
        if self._clock() < self._person_until:
            return self._person_until
        if self._holder is None:
            return self._clock()
        if self._active:
            return float("inf")
        free = min(self._last_end + self.idle_s, self._held_since + self.max_hold_s)
        if self._holder.pid is not None:
            free = min(free, self._clock() + _EXIT_POLL_S)  # look again whether it has exited
        return free

    def _take(self, client: Client) -> None:
        now = self._clock()
        if self._holder is not client:
            if self._holder is not None:
                log.info("phone passes from %s to %s", self._holder.label, client.label)
            else:
                log.info("phone taken by %s", client.label)
            for other in self._clients.values():
                if other is not client:
                    self._disturb(other, client.label)
            self._holder, self._held_since = client, now
            self._last_user = client.label
        elif not self._active and now - self._last_end >= self.idle_s:
            self._held_since = now  # back after a break: a new stretch of use, not one long hold
        self._active += 1
        client.seen = now

    # --- release and the person ---------------------------------------------------------

    async def release(self, client: Client) -> bool:
        """Give the phone up now instead of after IDLE_S. False if this client did not hold it."""
        async with self._cond:
            if self._holder is not client:
                return False
            if self._active:
                return False  # a call of its own is still running; idle time takes over after it
            self._holder = None
            self._cond.notify_all()
            log.info("%s released the phone", client.label)
            return True

    async def hold_for_person(self, seconds: float) -> None:
        """The person uses the phone by hand: no client gets it for this long (0 ends it)."""
        async with self._cond:
            person = self.client(PERSON, "the person using the phone")
            if seconds > 0:
                self._person_until = self._clock() + seconds
                # Every client has to look again before a blind input afterwards.
                for other in self._clients.values():
                    if other is not person:
                        self._disturb(other, person.label)
                self._last_user = person.label
            else:
                self._person_until = 0.0
            self._cond.notify_all()

    # --- reporting ----------------------------------------------------------------------

    def _holder_label(self) -> str:
        if self._clock() < self._person_until:
            return "the person using the phone (phonectl hold)"
        return self._holder.label if self._holder else "nobody"

    def _busy_message(self, waited: float) -> str:
        now = self._clock()
        if now < self._person_until:
            return (f"the phone is held for the person using it for another "
                    f"{self._person_until - now:.0f} s (phonectl hold); waited {waited:.0f} s")
        h = self._holder
        how = "a call is running" if self._active else f"its last call ended {now - self._last_end:.0f} s ago"
        return (f"the phone is in use by {h.label if h else 'another client'} ({how}, holding it for "
                f"{now - self._held_since:.0f} s); waited {waited:.0f} s. It is free {self.idle_s:.0f} s "
                "after the holder's last call: try again later")

    def describe(self, viewer: Optional[Client] = None) -> str:
        """One line on who has the phone, for status."""
        now = self._clock()
        if now < self._person_until:
            return f"held for the person using it for another {self._person_until - now:.0f} s (phonectl hold)"
        h = self._holder
        if h is None or (not self._active and now - self._last_end >= self.idle_s):
            line = "free"
        else:
            who = "you" if viewer is h else h.label
            how = "a call is running" if self._active else (
                f"last call {now - self._last_end:.0f} s ago, free in {self.idle_s - (now - self._last_end):.0f} s")
            line = f"in use by {who} ({how})"
        if self._queue:
            line += f"; waiting: {', '.join(c.label for c in self._queue)}"
        return line


# --- telling clients apart --------------------------------------------------------------

@dataclass
class _Peer:
    expiry: float
    pid: int
    label: str
    ancestors: list[int]  # parent first, up to (not including) launchd


_peer_cache: dict[int, _Peer] = {}  # client port -> the process behind it
# Renewed on every request: a connection in use keeps its entry, and a port left idle this
# long is looked up again (another process may have it by then). Above the 5 s keep-alive.
_PEER_TTL_S = 10.0


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except subprocess.TimeoutExpired:
        log.warning("%s took over 5 s; the client is told apart by its address only", cmd[0])
        return ""


def _peer_process(port: int) -> Optional[_Peer]:
    """The local process whose TCP connection uses this client port, and its ancestors."""
    me = os.getpid()
    pid = None
    name = ""
    # +c 0: the whole command name; lsof cuts it to 9 characters otherwise.
    for line in _run("lsof", "+c", "0", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED", "-Fpc").splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("c") and pid is not None and pid != me:
            name = line[1:]
            break
    else:
        return None
    cwd = next((l[1:] for l in _run("lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn").splitlines()
                if l.startswith("n")), "")
    where = f" in {Path(cwd).name}" if cwd and cwd != "/" else ""
    parent = {}
    for row in _run("ps", "-A", "-o", "pid=,ppid=").splitlines():
        fields = row.split()
        if len(fields) == 2:
            parent[int(fields[0])] = int(fields[1])
    ancestors, p = [], parent.get(pid, 1)
    while p > 1 and p not in ancestors:
        ancestors.append(p)
        p = parent.get(p, 1)
    return _Peer(0.0, pid, f"{name}{where} (pid {pid})", ancestors)


async def identify(arbiter: Arbiter, peer: Optional[tuple[str, int]], name: Optional[str]) -> tuple[Client, bool]:
    """The client behind a request (its X-Phonectl-Client name, else its process), and
    whether the request is the client's own: False for a process the client started, which
    acts as the client but must not give up the phone the client holds."""
    if name:
        name = name.strip()[:60]
        return arbiter.client(f"name:{name}", name), True
    if peer is None:
        return arbiter.client("local", "the local client"), True
    host, port = peer
    now = time.monotonic()
    hit = _peer_cache.get(port)
    if hit is None or hit.expiry < now:
        found = await asyncio.to_thread(_peer_process, port)
        if found is None:
            # One record per host, not per connection: a client reconnecting on a new port is
            # still the same client (all of them are on 127.0.0.1, so they share it).
            return arbiter.client(f"addr:{host}", f"a client at {host}"), True
        for p in [p for p, v in _peer_cache.items() if v.expiry < now]:
            del _peer_cache[p]
        hit = _peer_cache[port] = found
    hit.expiry = now + _PEER_TTL_S
    for pid in [hit.pid, *hit.ancestors]:  # the process itself, else the nearest client that started it
        c = arbiter.known(f"pid:{pid}")
        if c is not None:
            return arbiter.client(c.key, c.label, c.pid), pid == hit.pid
    return arbiter.client(f"pid:{hit.pid}", hit.label, hit.pid), True
