"""Who may use the phone right now, when several clients share one server.

The phone has one screen, so two agents driving it at once take turns call by call and act on
each other's screens. Here a client that starts using the phone holds it for its whole task,
until it says it is done: an agent's end-of-turn hook (done), a script yielding between two
units of work (yield_to_waiter), its process exiting, or a release. The server does not guess
where a task ends from the pauses between calls: an agent thinks between steps for a few
seconds or a minute, which looks the same as having finished. Only a holder that made no call
for IDLE_S while someone waits is taken to have forgotten to say so. A call from anyone else
meanwhile waits in line instead of landing on the holder's screen. A wait lasts at most WAIT_S,
below every client's own tool-call timeout; then the call ends with StillWaiting, having done
nothing, and the client keeps its place in line for KEEP_PLACE_S so that calling again carries
on from there. Agents told plainly that this is not a failure call again (measured), so they
neither stop nor lose their turn, whatever their own timeout.

The holder is never cut off while it keeps working, except that after MAX_HOLD_S it lets a
waiting client go first. A long-running script gives the phone up between two units of work
(yield_to_waiter), so others need not wait for all of it. A client that lost the phone to
someone else is told so on its next blind input (a tap at coordinates read before), which is
then not sent: see Lease.stale_by. What a client has seen is kept per process (see _View).

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

# A holder that made no call for this long, while someone waits, has finished without saying
# so (an agent without an end-of-turn hook, or one whose hook did not get through). Longer than
# an agent's thinking between two steps; a holder that pauses longer mid-task (to write a draft,
# say) loses the phone, and gets back in line on its next call.
IDLE_S = float(os.environ.get("PHONECTL_IDLE", "90"))
# A place whose call has just run out of waiting still holds the line this long: the client is
# calling again (a model step, a few seconds), and the phone must not go to someone behind it
# in that gap.
RETRY_HOLD_S = 15.0
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
    # The screen version (Arbiter._version) any of its processes last looked at: what a new
    # process of it starts from (a tool run once per call is a new process every time).
    looked_at: Optional[int] = None


@dataclass(eq=False)
class _View:
    """What one process of a client last saw of the screen. Kept per process, not per client: a
    script an agent runs is the agent's own action for taking turns, but the script looking at
    the screen does not show the agent the screen, and its inputs change what the agent saw."""

    # Who changed the screen since this process last looked at it; None if nobody.
    stale_by: Optional[str] = None
    changes: int = 0  # how many times stale_by was set
    # It opened an app itself since it went stale: text it finds on the screen is in its own
    # app again (tap_text may go), though coordinates it read before may still be off.
    relaunched: bool = False


@dataclass(eq=False)
class _Place:
    """One call's place in line. A client can have several: an agent and a script it started
    are one client, and both may be waiting."""

    client: Client
    # None while a call waits in it. Set when that call ran out of waiting: the place is kept
    # until then for the client's next call.
    away_until: Optional[float] = None
    away_since: float = 0.0


@dataclass
class Lease:
    """Handed to a call that got the phone."""

    client: Client
    view: _View  # what the calling process last saw
    # Set when someone else changed the screen since this process last looked at it, so
    # coordinates it read before may point at something else now.
    stale_by: Optional[str]
    changes: int  # the view's count when the call started
    relaunched: bool  # the process opened an app itself since it went stale


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
                 retry_hold_s: float = RETRY_HOLD_S, sharing: bool = True):
        self.idle_s, self.wait_s, self.max_hold_s = idle_s, wait_s, max_hold_s
        self.keep_place_s = keep_place_s
        self.retry_hold_s = retry_hold_s
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
        self._queue: list[_Place] = []  # first come first; some between calls
        self._person_until = 0.0
        # Who changed the screen last; a process seen for the first time has not looked since.
        self._last_user: Optional[str] = None
        self._views: dict[tuple[str, int], _View] = {}  # (client key, pid or 0) -> what it saw
        self._version = 0  # how many times the screen was changed
        self._last_changer: Optional[str] = None  # the key of the client that changed it last

    def assume_used_before(self) -> None:
        """The phone may have been used before this server started (a restart): every client's
        first blind input has to be preceded by a look."""
        self._last_user = self._last_user or "someone before this server started"

    def _by(self, label: str, changer: Optional[str], viewer: str) -> str:
        if changer == viewer:
            return f"{label}, in another process of yours (a script or helper you ran)"
        return label

    def _view(self, client: Client, pid: Optional[int]) -> _View:
        key = (client.key, pid or 0)
        v = self._views.get(key)
        if v is None:
            # A new process starts from what its client last saw: fresh if one of the client's
            # processes has looked since the screen last changed (a tool that runs once per call
            # looks in one process and taps in the next), else it has not seen what changed.
            # Known gap: the server cannot tell which process's output the agent read, so a new
            # process is let through even when the agent's coordinates came from an older look
            # by another of its processes.
            fresh = client.looked_at is not None and client.looked_at == self._version
            stale = None if fresh or self._last_user is None else \
                self._by(self._last_user, self._last_changer, client.key)
            v = self._views[key] = _View(stale_by=stale)
        return v

    def _changed(self, by: Client, view: Optional[_View]) -> None:
        """by (in the process whose view is `view`) is about to change the screen: every other
        process has to look again before a blind input, and an app it opened is no longer
        known to be in front."""
        for (key, _), v in self._views.items():
            if v is view:
                continue
            v.stale_by = self._by(by.label, by.key, key)
            v.changes += 1
            v.relaunched = False
        self._version += 1
        self._last_user, self._last_changer = by.label, by.key

    # --- identity -----------------------------------------------------------------------

    def known(self, key: str) -> Optional[Client]:
        return self._clients.get(key)

    def client(self, key: str, label: str, pid: Optional[int] = None) -> Client:
        """The record for this client, made on first sight."""
        now = self._clock()
        for k in [k for k, c in self._clients.items()
                  if now - c.seen > FORGET_S and c is not self._holder
                  and not any(p.client is c for p in self._queue)]:
            del self._clients[k]
        for k in [k for k in self._views
                  if k[0] not in self._clients or (k[1] and not _alive(k[1]))]:
            del self._views[k]  # forgotten clients, and processes that have exited
        c = self._clients.get(key)
        if c is None:
            c = self._clients[key] = Client(key, label, now, pid)
        c.label = label
        if pid is not None:
            c.pid = pid  # a named client's latest process
        return c

    # --- pace ---------------------------------------------------------------------------

    def grace(self) -> float:
        """How long a holder may make no call before it counts as done while someone waits.
        Fixed: a pause tells nothing about whether a task is over (see the module notes)."""
        return self.idle_s

    def _new_stretch(self, now: float) -> None:
        self._held_since = now

    # --- use ----------------------------------------------------------------------------

    @asynccontextmanager
    async def use(self, client: Client, abandoned: Optional[asyncio.Event] = None, *,
                  pid: Optional[int] = None) -> AsyncIterator[Lease]:
        """Hold the phone for one call, waiting in line for it if someone else has it. Raises
        StillWaiting after wait_s, and Abandoned once `abandoned` is set (see abandon()). pid is
        the calling process (what it has seen is kept per process). A call that goes on to
        change the screen says so with acting(), once it has passed its checks."""
        await self._acquire(client, abandoned)
        view = self._view(client, pid)
        lease = Lease(client, view, view.stale_by, view.changes, view.relaunched)
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

    def acting(self, lease: Lease) -> None:
        """The call is about to change the screen (it passed its checks; a refused call changes
        nothing): every other process has to look again."""
        self._changed(lease.client, lease.view)
        if lease.stale_by is None:
            # It acted on a screen it had seen: the client knows what is there now, so a new
            # process of it (the next run of a per-call tool) starts fresh after this.
            lease.client.looked_at = self._version

    def saw_screen(self, lease: Lease) -> None:
        """The call looked at the screen: the process's coordinates are current, unless the
        screen was changed by someone else while the call ran (the person took the phone)."""
        if lease.view.changes == lease.changes:
            lease.view.stale_by, lease.view.relaunched = None, False
            lease.client.looked_at = self._version

    def relaunched(self, lease: Lease) -> None:
        """The call opened an app: text the process finds on the screen is in its own app again."""
        if lease.view.changes == lease.changes and lease.view.stale_by is not None:
            lease.view.relaunched = True

    def _drop_expired(self, now: float) -> None:
        for p in [p for p in self._queue if p.away_until is not None and now >= p.away_until]:
            self._queue.remove(p)
            log.info("%s left the line: it did not call again", p.client.label)

    def _holds_line(self, p: _Place, now: float) -> bool:
        """A call waits in this place, or its client is calling again right now. A place whose
        client has been away longer holds up no one: the phone is not kept idle for it."""
        return p.away_until is None or now - p.away_since < self.retry_hold_s

    def _first_in_line(self, place: Optional[_Place]) -> bool:
        """No place that holds the line is ahead of this one (None: a call not in line yet)."""
        now = self._clock()
        for p in self._queue:
            if p is place:
                return True
            if self._holds_line(p, now):
                return False
        return True

    def _others_wait(self, client: Client) -> bool:
        now = self._clock()
        return any(p.client is not client and self._holds_line(p, now) for p in self._queue)

    def _window_ends(self, now: float) -> float:
        """When the next place that holds the line only for its client's retry stops holding it."""
        return min((p.away_since + self.retry_hold_s for p in self._queue
                    if p.away_until is not None and now - p.away_since < self.retry_hold_s), default=float("inf"))

    async def _acquire(self, client: Client, abandoned: Optional[asyncio.Event]) -> None:
        async with self._cond:
            now = self._clock()
            self._drop_expired(now)
            if not self.sharing:
                self._take(client)
                return
            # A call of this client ran out of waiting before: this one takes that place.
            place = next((p for p in self._queue if p.client is client and p.away_until is not None), None)
            if place is not None:
                place.away_until = None
            else:
                if abandoned is not None and abandoned.is_set():
                    raise Abandoned("the client gave up on the call before it reached the phone")
                if self._may_take(client, self._first_in_line(None)):
                    self._take(client)
                    return
                place = _Place(client)
                self._queue.append(place)
                log.info("%s waits for the phone (held by %s)", client.label, self._holder_label())
            deadline = now + self.wait_s
            try:
                while True:
                    now = self._clock()
                    if abandoned is not None and abandoned.is_set():
                        raise Abandoned("the client gave up on the call while it waited in line")
                    if self._may_take(client, self._first_in_line(place)):
                        break
                    if now >= deadline:
                        place.away_until, place.away_since = now + self.keep_place_s, now
                        raise StillWaiting(self._wait_detail(), self._queue.index(place) + 1, self.keep_place_s)
                    # Wake for a release, a call ending, the holder's pause running out, or a
                    # place ahead no longer holding the line. A phone free now but not to this
                    # call is held by such a place: its window's end is the next chance.
                    free = self._free_at(now)
                    if free <= now:
                        free = self._window_ends(now)
                    wake = min(deadline, free, self._window_ends(now))
                    try:
                        async with asyncio.timeout(max(wake - now, 0.01)):
                            await self._cond.wait()
                    except TimeoutError:
                        pass
            finally:
                if place.away_until is None:  # got the phone, or gave up: out of the line
                    self._queue.remove(place)
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
        done = (paused >= self.grace() or now - self._held_since >= self.max_hold_s
                or self._holder_exited())
        return first_in_line and done

    def _holder_exited(self) -> bool:
        """The holder's process is gone (a script or a one-shot agent run that ended)."""
        h = self._holder
        return h is not None and h.pid is not None and not _alive(h.pid)

    def _free_at(self, now: Optional[float] = None) -> float:
        """Earliest time the phone may change hands without anyone releasing it."""
        now = self._clock() if now is None else now
        if now < self._person_until:
            return self._person_until
        if self._holder is None:
            return now
        if self._active:
            return float("inf")
        free = min(self._last_end + self.grace(), self._held_since + self.max_hold_s)
        if self._holder.pid is not None:
            free = min(free, now + _EXIT_POLL_S)  # look again whether it has exited
        return free

    def _take(self, client: Client) -> None:
        now = self._clock()
        if self._holder is not client:
            if self._holder is not None:
                log.info("phone passes from %s to %s", self._holder.label, client.label)
            else:
                log.info("phone taken by %s", client.label)
            self._holder = client
            self._new_stretch(now)
            # Its turn has come: places it kept from earlier calls are used up, or it could go
            # ahead of the line again right after giving the phone up.
            self._queue = [p for p in self._queue if not (p.client is client and p.away_until is not None)]
        elif not self._active and now - self._last_end >= self.grace():
            self._new_stretch(now)  # back after a break: a new stretch of use, not one long hold
        self._active += 1
        client.seen = now

    # --- giving the phone up, and the person ----------------------------------------------

    async def release(self, client: Client) -> bool:
        """Give the phone up now instead of after a pause. False if this client did not hold it.
        Places it kept in line are given up too: it is done."""
        async with self._cond:
            self._queue = [p for p in self._queue if not (p.client is client and p.away_until is not None)]
            if self._holder is not client:
                return False
            if self._active:
                return False  # a call of its own is still running; its pause counts after it
            self._holder = None
            self._cond.notify_all()
            log.info("%s released the phone", client.label)
            return True

    async def done(self, client: Client) -> bool:
        """The client's turn ended (its end-of-turn hook): give the phone up, unless another
        process of it that has used the phone is still running (a script it left working in
        the background). Then the phone goes by the client's pace as usual."""
        for key, pid in self._views:
            if key == client.key and pid and pid != client.pid and _alive(pid):
                return False
        return await self.release(client)

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
                # Every process has to look again before a blind input afterwards.
                self._changed(person, None)
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
                p.client.label + (" (between calls)" if p.away_until is not None else "") for p in self._queue)
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


async def identify(arbiter: Arbiter, peer: Optional[tuple[str, int]],
                   name: Optional[str], create: bool = True) -> tuple[Optional[Client], bool, Optional[int]]:
    """The client behind a request (its X-Phonectl-Client name, else its process), whether the
    request is the client's own (False for a process the client started, which acts as the
    client but must not give up the phone the client holds), and the process that sent it."""
    hit = None
    if peer is not None:
        host, port = peer
        now = time.monotonic()
        hit = _peer_cache.get(port)
        if hit is None or hit.expiry < now:
            hit = await asyncio.to_thread(_peer_process, port)
            for p in [p for p, v in _peer_cache.items() if v.expiry < now]:
                del _peer_cache[p]
            if hit is not None:
                _peer_cache[port] = hit
        if hit is not None:
            hit.expiry = now + _PEER_TTL_S
    pid = hit.pid if hit is not None else None

    def get(key: str, label: str, own_pid: Optional[int] = None) -> Optional[Client]:
        if not create and arbiter.known(key) is None:
            return None
        return arbiter.client(key, label, own_pid)

    if name:
        # Told apart by name, but its process is known too: when it exits, it holds nothing.
        name = name.strip()[:60]
        return get(f"name:{name}", name, pid), True, pid
    if peer is None:
        return get("local", "the local client"), True, None
    if hit is None:
        # One record per host, not per connection: a client reconnecting on a new port is
        # still the same client (all of them are on 127.0.0.1, so they share it).
        return get(f"addr:{peer[0]}", f"a client at {peer[0]}"), True, None
    for p in [hit.pid, *hit.ancestors]:  # the process itself, else the nearest client that started it
        c = arbiter.known(f"pid:{p}")
        if c is not None:
            return arbiter.client(c.key, c.label), p == hit.pid, hit.pid
    return get(f"pid:{hit.pid}", hit.label, hit.pid), True, hit.pid
