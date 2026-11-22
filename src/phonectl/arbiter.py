"""Who may use the phone right now, when several clients share one server.

The phone has one screen, so two agents driving it at once take turns call by call and act on
each other's screens. Here a client that starts using the phone holds it until it pauses for
longer than its own pace says it is still working (see grace()); a call from anyone else
meanwhile waits in line instead of landing on the holder's screen. A wait lasts at most WAIT_S,
below every client's own tool-call timeout; then the call ends with StillWaiting, having done
nothing, and the client keeps its place in line for KEEP_PLACE_S so that calling again carries
on from there. Agents told plainly that this is not a failure call again (measured), so they
neither stop nor lose their turn, whatever their own timeout.

The holder is never cut off while it keeps working, except that after MAX_HOLD_S it lets a
waiting client go first. A long-running script gives the phone up between two units of work
(yield_to_waiter), so others need not wait for all of it. A client that lost the phone to
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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

log = logging.getLogger(__name__)

# A holder that paused this long has finished with the phone, whatever its pace: the most
# grace() gives. A holder's pause is judged by its own recent pace (see grace()), between
# GRACE_MIN_S and this; GRACE_START_S until there is a pace to go by.
IDLE_S = float(os.environ.get("PHONECTL_IDLE", "90"))
GRACE_MIN_S = 10.0
GRACE_START_S = 60.0
# How long a call waits in line before it ends with StillWaiting. Below the shortest tool-call
# timeout of the clients in use (DeepSeek Harness: 60 s by default; Gemini: 180 s; Claude
# Code: 300 s without progress), so the client hears from the server, not from its own timer.
WAIT_S = float(os.environ.get("PHONECTL_WAIT", "50"))
# How long a client that ran out of waiting keeps its place in line for its next call: an
# agent needs a model step to call again, a slow one over a minute.
KEEP_PLACE_S = 120.0
# After holding this long without a break, the holder lets a waiting client go first. Long:
# a long-running script is expected to yield between units of work, and cutting one off in the
# middle of a unit costs that unit.
MAX_HOLD_S = float(os.environ.get("PHONECTL_MAX_HOLD", "1800"))
# Clients not seen for this long are forgotten.
FORGET_S = 3600.0
# The person using the phone by hand (phonectl hold).
PERSON = "person"
# While the holder's process is known, a waiting call checks this often whether it has exited.
_EXIT_POLL_S = 1.0


class StillWaiting(RuntimeError):
    """The phone stayed in use by someone else for the whole wait; nothing was done, and the
    client keeps its place in line for its next call."""

    def __init__(self, detail: str, position: int, keep_s: float):
        super().__init__(detail)
        self.detail, self.position, self.keep_s = detail, position, keep_s


class Abandoned(RuntimeError):
    """The client gave up on a call while it waited in line (it cancelled the request)."""


@dataclass
class Client:
    key: str  # what tells clients apart: "pid:123", "name:<header>", "person", "local"
    label: str  # shown to other clients and in the log
    seen: float = 0.0  # monotonic time of the last call
    pid: Optional[int] = None  # its process, when known: once that exits, it holds nothing
    # Label of whoever used the phone after this client last saw the screen; None if nobody.
    disturbed_by: Optional[str] = None
    disturbances: int = 0  # how many times disturbed_by was set
    # In line but between calls (its last one ran out of waiting): keeps its place until then.
    away_until: Optional[float] = None


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
                 clock: Callable[[], float] = time.monotonic, keep_place_s: float = KEEP_PLACE_S,
                 grace_min_s: float = GRACE_MIN_S, grace_start_s: float = GRACE_START_S,
                 sharing: bool = True):
        self.idle_s, self.wait_s, self.max_hold_s = idle_s, wait_s, max_hold_s
        self.keep_place_s, self.grace_min_s, self.grace_start_s = keep_place_s, grace_min_s, grace_start_s
        # False only to measure what sharing prevents (PHONECTL_SHARING=off, for benchmarks):
        # every call goes ahead at once, as before this module existed; blind inputs on a
        # screen someone else changed are still noticed (Lease.stale_by) but not refused.
        self.sharing = sharing
        self._clock = clock
        self._cond = asyncio.Condition()
        self._clients: dict[str, Client] = {}
        self._holder: Optional[Client] = None
        self._held_since = 0.0
        self._last_end = 0.0  # when the holder's last call ended
        self._active = 0  # calls of the holder in progress
        self._queue: list[Client] = []  # waiting, first come first; some may be between calls
        self._person_until = 0.0
        # Who used the phone last; a client seen for the first time has not looked since.
        self._last_user: Optional[str] = None
        # The holder's pace over its current stretch of use: smoothed pause between its calls
        # and the pauses' mean deviation from it, and how many pauses went into them.
        self._pace: Optional[tuple[float, float]] = None
        self._paces = 0

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
        for k in [k for k, c in self._clients.items()
                  if now - c.seen > FORGET_S and c is not self._holder and c not in self._queue]:
            del self._clients[k]
        c = self._clients.get(key)
        if c is None:
            c = self._clients[key] = Client(key, label, now, pid)
            # New, or forgotten after FORGET_S: it has not seen what the last user left.
            if self._last_user is not None:
                self._disturb(c, self._last_user)
        c.label = label
        return c

    # --- pace ---------------------------------------------------------------------------

    def grace(self) -> float:
        """How long the holder may pause before it counts as done with the phone: its smoothed
        pause plus four mean deviations (how TCP tells a lost packet from a slow one), kept within
        grace_min_s..idle_s. Its pace in this stretch of use is what counts, not the client's
        habits: the same agent thinks longer on one task than another, and two agents doing the
        same task think at different speeds."""
        if self._pace is None or self._paces < 2:
            estimate = self.grace_start_s
        else:
            average, deviation = self._pace
            estimate = average + 4 * deviation
        return min(self.idle_s, max(self.grace_min_s, estimate))

    def _note_pause(self, pause: float) -> None:
        if self._pace is None:
            self._pace = (pause, pause / 2)
        else:
            average, deviation = self._pace
            self._pace = (0.875 * average + 0.125 * pause, 0.75 * deviation + 0.25 * abs(average - pause))
        self._paces += 1

    def _new_stretch(self, now: float) -> None:
        self._held_since = now
        self._pace, self._paces = None, 0

    # --- use ----------------------------------------------------------------------------

    @asynccontextmanager
    async def use(self, client: Client, abandoned: Optional[asyncio.Event] = None) -> AsyncIterator[Lease]:
        """Hold the phone for one call, waiting in line for it if someone else has it. Raises
        StillWaiting after wait_s, and Abandoned once `abandoned` is set (see abandon())."""
        await self._acquire(client, abandoned)
        lease = Lease(client, client.disturbed_by, client.disturbances)
        try:
            yield lease
        finally:
            async with self._cond:
                self._active -= 1
                client.seen = self._last_end = self._clock()
                self._cond.notify_all()

    async def abandon(self, flag: asyncio.Event) -> None:
        """The client hung up on a call that is waiting in line: end that wait, so the input it
        no longer waits for is never sent."""
        async with self._cond:
            flag.set()
            self._cond.notify_all()

    def saw_screen(self, lease: Lease) -> None:
        """The call looked at the screen: the client's coordinates are current, unless it was
        disturbed again while the call ran (the person took the phone meanwhile)."""
        if lease.client.disturbances == lease.disturbances:
            lease.client.disturbed_by = None

    def _drop_expired(self, now: float) -> None:
        for c in [c for c in self._queue if c.away_until is not None and now >= c.away_until]:
            self._queue.remove(c)
            c.away_until = None
            log.info("%s left the line: it did not call again", c.label)

    def _first_in_line(self, client: Client) -> bool:
        """Nobody who is waiting right now is ahead of client. One between calls holds up no one:
        the phone is not kept idle for it."""
        for c in self._queue:
            if c is client:
                return True
            if c.away_until is None:
                return False
        return True

    def _others_wait(self, client: Client) -> bool:
        return any(c is not client and c.away_until is None for c in self._queue)

    async def _acquire(self, client: Client, abandoned: Optional[asyncio.Event]) -> None:
        async with self._cond:
            now = self._clock()
            self._drop_expired(now)
            if not self.sharing:
                self._take(client)
                return
            back = client in self._queue  # its last call ran out of waiting: same place again
            client.away_until = None
            if not back:
                if self._may_take(client, self._first_in_line(client)):
                    self._take(client)
                    return
                self._queue.append(client)
                log.info("%s waits for the phone (held by %s)", client.label, self._holder_label())
            deadline = now + self.wait_s
            try:
                while True:
                    now = self._clock()
                    if abandoned is not None and abandoned.is_set():
                        raise Abandoned("the client gave up on the call while it waited in line")
                    if self._may_take(client, self._first_in_line(client)):
                        break
                    if now >= deadline:
                        client.away_until = now + self.keep_place_s
                        raise StillWaiting(self._wait_detail(), self._queue.index(client) + 1, self.keep_place_s)
                    # Wake for a release, a call ending, or the holder's pause running out.
                    wake = min(deadline, self._free_at())
                    try:
                        async with asyncio.timeout(max(wake - now, 0.01)):
                            await self._cond.wait()
                    except TimeoutError:
                        pass
            finally:
                if client.away_until is None:  # got the phone, or gave up: out of the line
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
            return not self._others_wait(client) or now - self._held_since < self.max_hold_s or first_in_line
        if self._active:
            return False
        paused = now - self._last_end
        done = paused >= self.grace() or now - self._held_since >= self.max_hold_s or self._holder_exited()
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
        free = min(self._last_end + self.grace(), self._held_since + self.max_hold_s)
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
            self._holder = client
            self._new_stretch(now)
            self._last_user = client.label
        elif not self._active:
            pause = now - self._last_end
            if pause >= self.grace():
                self._new_stretch(now)  # back after a break: a new stretch of use, not one long hold
            else:
                self._note_pause(pause)
        self._active += 1
        client.seen = now

    # --- giving the phone up, and the person ----------------------------------------------

    async def release(self, client: Client) -> bool:
        """Give the phone up now instead of after a pause. False if this client did not hold it."""
        async with self._cond:
            if self._holder is not client:
                return False
            if self._active:
                return False  # a call of its own is still running; its pause counts after it
            self._holder = None
            self._cond.notify_all()
            log.info("%s released the phone", client.label)
            return True

    async def yield_to_waiter(self, client: Client) -> bool:
        """Between two units of work: give the phone up if someone else is waiting for it, else
        keep it. True if it was given up; the client's next call then waits in line."""
        async with self._cond:
            if self._holder is not client or self._active or not self._others_wait(client):
                return False
            self._holder = None
            self._cond.notify_all()
            log.info("%s yielded the phone to the line", client.label)
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

    def _wait_detail(self) -> str:
        """Who has the phone, for a client that ran out of waiting."""
        now = self._clock()
        if now < self._person_until:
            return (f"the person using the phone holds it for another "
                    f"{self._person_until - now:.0f} s (phonectl hold)")
        h = self._holder
        if h is None:
            return "the phone is going to a client ahead of you in line"
        how = "a call of theirs is running" if self._active else f"their last call ended {now - self._last_end:.0f} s ago"
        return f"{h.label} is using the phone ({how})"

    def describe(self, viewer: Optional[Client] = None) -> str:
        """One line on who has the phone, for status."""
        now = self._clock()
        self._drop_expired(now)
        if now < self._person_until:
            return f"held for the person using it for another {self._person_until - now:.0f} s (phonectl hold)"
        h = self._holder
        grace = self.grace()
        if h is None or (not self._active and now - self._last_end >= grace):
            line = "free"
        else:
            who = "you" if viewer is h else h.label
            how = "a call is running" if self._active else (
                f"last call {now - self._last_end:.0f} s ago, free in {grace - (now - self._last_end):.0f} s")
            line = f"in use by {who} ({how})"
        if self._queue:
            line += "; waiting: " + ", ".join(
                c.label + (" (between calls)" if c.away_until is not None else "") for c in self._queue)
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
