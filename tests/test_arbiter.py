import asyncio
import time

import pytest

from phonectl import arbiter as arbiter_module
from phonectl.arbiter import Abandoned, Arbiter, StillWaiting


def run(coro):
    return asyncio.run(coro)


async def call(arb, client, seconds=0.0, log=None, acts=True):
    """One tool call by client that takes `seconds` while holding the phone; with acts, one that
    changes the screen (a tap), else one that only looks."""
    async with arb.use(client) as lease:
        if acts:
            arb.acting(lease)
        if log is not None:
            log.append((client.key, time.monotonic()))
        await asyncio.sleep(seconds)
        return lease


def test_holder_keeps_the_phone_and_others_wait_for_its_idle_time():
    async def main():
        arb = Arbiter(idle_s=0.3, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        await call(arb, a)
        t0 = time.monotonic()
        await call(arb, b)
        return time.monotonic() - t0

    assert 0.25 < run(main()) < 1.0


def test_a_call_in_progress_is_never_cut_off():
    async def main():
        arb = Arbiter(idle_s=0.05, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        log = []
        long_call = asyncio.create_task(call(arb, a, 0.4, log))
        await asyncio.sleep(0.05)
        await call(arb, b, 0, log)
        await long_call
        return log, time.monotonic()

    log, _ = run(main())
    (ka, ta), (kb, tb) = log
    assert (ka, kb) == ("a", "b") and tb - ta >= 0.4


def test_a_call_that_ran_out_of_waiting_did_nothing_and_is_told_who_has_the_phone():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=0.3)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        await call(arb, a)
        with pytest.raises(StillWaiting, match=r"A is using the phone") as w:
            await call(arb, b)
        assert w.value.position == 1
        # The holder goes on meanwhile, and gives the phone up as before.
        await call(arb, a)
        assert await arb.release(a)

    run(main())


def test_calling_again_keeps_the_place_in_line():
    async def main():
        arb = Arbiter(idle_s=0.1, wait_s=0.2, keep_place_s=5)
        a, b, c = (arb.client(k, k.upper()) for k in "abc")
        log = []
        ta = asyncio.create_task(call(arb, a, 0.6))  # a long call: nobody gets in before it ends
        await asyncio.sleep(0.02)
        with pytest.raises(StillWaiting):
            await call(arb, b)  # b's agent takes a model step before calling again
        arb.wait_s = 5
        tc = asyncio.create_task(call(arb, c, 0, log))
        await asyncio.sleep(0.05)
        tb = asyncio.create_task(call(arb, b, 0, log))  # b is back: ahead of c, who came later
        await asyncio.gather(ta, tb, tc)
        return [k for k, _ in log]

    assert run(main()) == ["b", "c"]


def test_a_client_between_calls_holds_up_no_one():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=0.2, keep_place_s=5, retry_hold_s=0.01)
        a, b, c = (arb.client(k, k.upper()) for k in "abc")
        await call(arb, a)
        with pytest.raises(StillWaiting):
            await call(arb, b)
        tc = asyncio.create_task(call(arb, c))
        await asyncio.sleep(0.05)
        assert "B (between calls)" in arb.describe()
        assert await arb.release(a)
        await asyncio.wait_for(tc, 0.1)  # c goes now; the phone does not sit idle waiting for b

    run(main())


def test_a_kept_place_runs_out():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=0.1, keep_place_s=0.2)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        await call(arb, a)
        with pytest.raises(StillWaiting):
            await call(arb, b)
        assert "waiting: B" in arb.describe()
        await asyncio.sleep(0.25)
        return arb.describe()

    assert "waiting" not in run(main())


def test_an_abandoned_wait_ends_without_the_phone_and_leaves_the_line():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        await call(arb, a)
        gave_up = asyncio.Event()

        async def b_call():
            async with arb.use(b, gave_up):
                return "got the phone"

        waiting = asyncio.create_task(b_call())
        await asyncio.sleep(0.05)
        await arb.abandon(gave_up)
        with pytest.raises(Abandoned):
            await waiting
        return arb.describe()

    assert "waiting" not in run(main())


def test_yield_gives_the_phone_up_only_to_someone_waiting():
    async def main():
        arb = Arbiter(idle_s=0.3, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        log = []
        await call(arb, a)
        assert not await arb.yield_to_waiter(a)  # nobody waits: a keeps it
        await call(arb, a, 0, log)
        tb = asyncio.create_task(call(arb, b, 0, log))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        assert await arb.yield_to_waiter(a)
        await tb
        waited = time.monotonic() - t0
        await call(arb, a, 0, log)  # a's next unit waits its turn
        return [k for k, _ in log], waited

    order, waited = run(main())
    assert order == ["a", "b", "a"] and waited < 0.2


def test_without_sharing_every_call_goes_at_once_and_crossings_show():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5, sharing=False)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        log = []
        arb.saw_screen(await call(arb, b))
        ta = asyncio.create_task(call(arb, a, 0.3, log))
        await asyncio.sleep(0.05)
        lease = await call(arb, b, 0, log)  # does not wait for a's call
        await ta
        return [k for k, _ in log], lease.stale_by

    order, stale = run(main())
    assert order == ["a", "b"] and stale == "A"


def test_waiters_are_served_in_order():
    async def main():
        arb = Arbiter(idle_s=0.1, wait_s=5)
        a, b, c = (arb.client(k, k.upper()) for k in "abc")
        log = []
        await call(arb, a)
        tb = asyncio.create_task(call(arb, b, 0, log))
        await asyncio.sleep(0.02)
        tc = asyncio.create_task(call(arb, c, 0, log))
        await asyncio.gather(tb, tc)
        return [k for k, _ in log]

    assert run(main()) == ["b", "c"]


def test_blind_input_is_flagged_after_someone_else_used_the_phone():
    async def main():
        arb = Arbiter(idle_s=0.05, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        first = await call(arb, a)
        await call(arb, b)  # after a's idle time
        again = await call(arb, a)
        arb.saw_screen(again)  # that call looked at the screen
        after_look = await call(arb, a)
        return first.stale_by, again.stale_by, after_look.stale_by

    assert run(main()) == (None, "B", None)


def test_holder_that_held_too_long_lets_a_waiter_go_first():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5, max_hold_s=0.3)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        log = []

        async def busy_holder():
            for _ in range(20):
                await call(arb, a, 0.05, log)

        holder = asyncio.create_task(busy_holder())
        await asyncio.sleep(0.1)
        await call(arb, b, 0, log)
        await holder
        return [k for k, _ in log]

    order = run(main())
    i = order.index("b")
    assert 0 < i < len(order) - 1  # b got in while a still had calls to make


def test_person_hold_keeps_everyone_off_until_it_ends():
    async def main():
        arb = Arbiter(idle_s=0.01, wait_s=5)
        a = arb.client("a", "A")
        await call(arb, a)
        await arb.hold_for_person(10)
        assert arb.describe().startswith("held for the person")
        waiting = asyncio.create_task(call(arb, a))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        await arb.hold_for_person(0)
        lease = await waiting
        return lease.stale_by

    assert run(main()) == "the person using the phone"


def test_a_cancelled_wait_leaves_the_line():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5)
        a, b, c = (arb.client(k, k.upper()) for k in "abc")
        await call(arb, a)
        waiting = asyncio.create_task(call(arb, b))
        await asyncio.sleep(0.05)
        waiting.cancel()  # the client hung up
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert await arb.release(a)
        await asyncio.wait_for(call(arb, c), 1)  # not stuck behind b

    run(main())


def test_describe_names_the_holder_and_the_line():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        assert arb.describe() == "free"
        await call(arb, a)
        mine, theirs = arb.describe(a), arb.describe(b)
        waiting = asyncio.create_task(call(arb, b))
        await asyncio.sleep(0.05)
        line = arb.describe(b)
        waiting.cancel()
        return mine, theirs, line

    mine, theirs, line = run(main())
    assert mine.startswith("in use by you")
    assert theirs.startswith("in use by A (last call")
    assert line.endswith("waiting: B")


def test_a_holder_back_after_a_break_starts_a_new_stretch():
    # Held long ago, idle since, nobody else used it: coming back is not "holding for too long".
    async def main():
        arb = Arbiter(idle_s=0.2, wait_s=5, max_hold_s=0.5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        log = []
        await call(arb, a)
        await asyncio.sleep(0.6)
        await call(arb, a, 0, log)
        waiting = asyncio.create_task(call(arb, b, 0, log))
        await asyncio.sleep(0.05)
        await call(arb, a, 0, log)  # a goes on: its stretch began 0.05 s ago
        await waiting
        return [k for k, _ in log]

    assert run(main()) == ["a", "a", "b"]


def test_a_look_during_which_the_person_took_the_phone_does_not_count():
    async def main():
        arb = Arbiter(idle_s=0.05, wait_s=5)
        a = arb.client("a", "A")
        async with arb.use(a) as lease:  # e.g. a wait_text running
            await arb.hold_for_person(10)
        arb.saw_screen(lease)  # the look began before the person took the phone
        await arb.hold_for_person(0)
        return (await call(arb, a)).stale_by

    assert run(main()) == "the person using the phone"


def test_a_forgotten_client_is_stale_when_it_comes_back(monkeypatch):
    monkeypatch.setattr(arbiter_module, "FORGET_S", 0.1)

    async def main():
        arb = Arbiter(idle_s=0.01, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        arb.saw_screen(await call(arb, a))
        await asyncio.sleep(0.05)
        await call(arb, b)
        await asyncio.sleep(0.15)
        await call(arb, b)  # a is forgotten here
        a_again = arb.client("a", "A")
        return a_again is a, (await call(arb, a_again)).stale_by

    assert run(main()) == (False, "B")


def test_a_client_seen_first_after_a_restart_must_look_first():
    async def main():
        arb = Arbiter(idle_s=0.01, wait_s=5)
        arb.assume_used_before()
        a = arb.client("a", "A")
        return (await call(arb, a)).stale_by

    assert run(main()) == "someone before this server started"


def test_two_calls_of_one_client_wait_side_by_side():
    # An agent and a script it started are one client; both may wait at once.
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        await call(arb, a)
        both = asyncio.gather(call(arb, b), call(arb, b))
        await asyncio.sleep(0.05)
        assert arb.describe().endswith("waiting: B, B")
        assert await arb.release(a)
        await both  # neither fails

    run(main())


def test_one_call_running_out_of_waiting_keeps_the_clients_other_call_in_line():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5, keep_place_s=5, retry_hold_s=0.01)
        a, b, c = (arb.client(k, k.upper()) for k in "abc")
        log = []
        await call(arb, a)
        arb.wait_s = 0.2
        short = asyncio.create_task(call(arb, b))
        await asyncio.sleep(0.02)
        arb.wait_s = 5
        long = asyncio.create_task(call(arb, b, 0, log))
        with pytest.raises(StillWaiting):
            await short
        tc = asyncio.create_task(call(arb, c, 0, log))
        await asyncio.sleep(0.05)
        assert "B (between calls), B, C" in arb.describe()
        arb.idle_s = 0.1  # b is quick to be done once it has had its turn
        assert await arb.release(a)
        await asyncio.gather(long, tc)
        return [k for k, _ in log]

    assert run(main()) == ["b", "c"]  # b's waiting call is still ahead of c


def test_a_place_kept_from_before_is_used_up_once_the_client_has_had_its_turn():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5, keep_place_s=5)
        x, z, w = (arb.client(k, k.upper()) for k in "xzw")
        log = []
        await call(arb, z)
        arb.wait_s = 0.1
        with pytest.raises(StillWaiting):
            await call(arb, x)  # x keeps a place at the front
        arb.wait_s = 5
        tx = asyncio.create_task(call(arb, x, 0, log))  # x's other call
        await asyncio.sleep(0.02)
        assert await arb.release(z)
        await tx  # x has had its turn
        tz = asyncio.create_task(call(arb, z, 0, log))
        tw = asyncio.create_task(call(arb, w, 0, log))
        await asyncio.sleep(0.05)
        assert await arb.yield_to_waiter(x)
        await tz
        arb.idle_s = 0.05
        tx2 = asyncio.create_task(call(arb, x, 0, log))  # must not jump ahead of w
        await asyncio.gather(tw, tx2)
        return [k for k, _ in log]

    assert run(main()) == ["x", "z", "w", "x"]


def test_a_call_cancelled_before_it_reached_the_phone_is_not_sent():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5)
        a = arb.client("a", "A")
        gave_up = asyncio.Event()
        await arb.abandon(gave_up)
        with pytest.raises(Abandoned):
            async with arb.use(a, gave_up):
                pass

    run(main())


def test_a_client_calling_again_is_not_overtaken_in_the_gap():
    # Its call ran out of waiting and its agent is calling again: someone behind it must not
    # take the phone in those seconds.
    async def main():
        arb = Arbiter(idle_s=10, wait_s=0.2, keep_place_s=5, retry_hold_s=0.5)
        a, b, c = (arb.client(k, k.upper()) for k in "abc")
        log = []
        await call(arb, a)
        with pytest.raises(StillWaiting):
            await call(arb, b)
        arb.wait_s = 5
        tc = asyncio.create_task(call(arb, c, 0, log))
        await asyncio.sleep(0.02)
        assert await arb.yield_to_waiter(a)  # b counts as waiting while it calls again
        await asyncio.sleep(0.1)
        tb = asyncio.create_task(call(arb, b, 0, log))
        arb.idle_s = 0.05
        await asyncio.gather(tb, tc)
        return [k for k, _ in log]

    assert run(main()) == ["b", "c"]


def test_looking_does_not_make_anyone_look_again():
    async def main():
        arb = Arbiter(idle_s=0.01, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        arb.saw_screen(await call(arb, a))
        async with arb.use(b):
            pass
        return (await call(arb, a)).stale_by

    assert run(main()) is None


def test_what_was_seen_is_kept_per_process():
    # An agent and a script it ran are one client, but the script's look does not show the
    # agent the screen, and the script's taps change what the agent saw.
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5)
        agent = arb.client("pid:1", "agent", 1)
        async with arb.use(agent, pid=1) as lease:
            arb.saw_screen(lease)
        async with arb.use(agent, pid=2) as lease:  # the script taps
            arb.acting(lease)
        async with arb.use(agent, pid=2) as lease:
            arb.saw_screen(lease)  # and looks
        async with arb.use(agent, pid=1) as lease:
            return lease.stale_by

    assert "another process of yours" in run(main())


def test_opening_its_own_app_again_is_remembered_until_it_looks():
    async def main():
        arb = Arbiter(idle_s=0.01, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        arb.saw_screen(await call(arb, a))
        await call(arb, b)  # b changes the screen
        lease = await call(arb, a)  # a opens its app
        arb.relaunched(lease)
        after_launch = await call(arb, a)
        arb.saw_screen(after_launch)
        return after_launch.stale_by, after_launch.relaunched, (await call(arb, a)).relaunched

    assert run(main()) == ("B", True, False)


def test_someone_elses_change_undoes_an_app_opened_again():
    async def main():
        arb = Arbiter(idle_s=0.01, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        arb.saw_screen(await call(arb, a))
        await call(arb, b)
        arb.relaunched(await call(arb, a))  # a opens its app again
        await call(arb, b)  # then b changes the screen once more
        return (await call(arb, a)).relaunched

    assert run(main()) is False


def test_a_new_process_starts_from_what_its_client_last_saw():
    # A tool that runs once per call looks in one process and taps in the next.
    async def main():
        arb = Arbiter(idle_s=0.01, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        await call(arb, b)  # someone changed the screen before
        async with arb.use(a, pid=11) as lease:
            arb.saw_screen(lease)  # process 11 looks, then exits
        async with arb.use(a, pid=12) as lease:  # process 12 taps
            fresh = lease.stale_by
        await call(arb, b)
        async with arb.use(a, pid=13) as lease:
            return fresh, lease.stale_by

    assert run(main()) == (None, "B")


def test_a_refused_call_changes_nothing():
    async def main():
        arb = Arbiter(idle_s=0.01, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        arb.saw_screen(await call(arb, b, acts=False))
        async with arb.use(a):  # a's blind tap is refused: no acting()
            pass
        return (await call(arb, b)).stale_by

    assert run(main()) is None


def test_the_end_of_turn_hook_does_not_free_a_phone_a_running_script_is_using():
    async def main():
        import os
        arb = Arbiter(idle_s=10, wait_s=5)
        agent = arb.client("pid:1", "agent", 1)
        async with arb.use(agent, pid=os.getpid()) as lease:  # a script of the agent, still running
            arb.acting(lease)
        async with arb.use(agent, pid=1) as lease:  # the agent itself looks last
            arb.saw_screen(lease)
        kept = not await arb.done(agent)
        arb._views.pop(("pid:1", os.getpid()))  # the script has exited
        return kept, await arb.done(agent)

    assert run(main()) == (True, True)


def test_a_per_call_tool_can_tap_then_type_without_looking_in_between():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=5)
        a = arb.client("a", "A")
        async with arb.use(a, pid=11) as lease:
            arb.saw_screen(lease)  # run 1: screenshot
        async with arb.use(a, pid=12) as lease:
            first = lease.stale_by
            arb.acting(lease)  # run 2: tap the field
        async with arb.use(a, pid=13) as lease:
            return first, lease.stale_by  # run 3: type

    assert run(main()) == (None, None)


def test_waiters_behind_a_retry_window_do_not_poll():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=0.1, keep_place_s=5, retry_hold_s=0.5)
        a, b, c = (arb.client(k, k.upper()) for k in "abc")
        await call(arb, a)
        with pytest.raises(StillWaiting):
            await call(arb, b)  # b's place holds the line for 0.5 s
        arb.wait_s = 5
        assert await arb.release(a)
        wakes = 0
        real = arb._may_take

        def counted(*args):
            nonlocal wakes
            wakes += 1
            return real(*args)
        arb._may_take = counted
        await call(arb, c)
        return wakes

    assert run(main()) < 10


def test_a_holder_keeps_the_phone_through_long_pauses_until_its_idle_time():
    # A pause says nothing about whether a task is over: only the fixed idle time does.
    async def main():
        arb = Arbiter(idle_s=0.6, wait_s=5)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        for pause in (0.01, 0.01, 0.4):  # quick steps, then a long thought
            await call(arb, a)
            await asyncio.sleep(pause)
        waiting = asyncio.create_task(call(arb, b))
        await asyncio.sleep(0.05)
        assert not waiting.done()  # 0.45 s after a's last call: still a's
        await call(arb, a)  # a goes on with its task
        t0 = time.monotonic()
        await waiting
        return time.monotonic() - t0

    assert 0.5 < run(main()) < 1.0  # b gets it only after a full idle time


def test_a_pause_longer_than_the_steps_before_it_does_not_end_the_holders_task():
    # The rule this replaced judged a holder by its own pace: after quick steps, a 30 s thought
    # counted as done. Now only the idle time (90 s) does.
    now = [0.0]
    arb = Arbiter(clock=lambda: now[0])
    a, b = arb.client("a", "A"), arb.client("b", "B")

    async def calls():
        for _ in range(5):
            await call(arb, a)
            now[0] += 1.0
    run(calls())
    now[0] += 30.0
    after_a_thought = arb._may_take(b, True)
    now[0] += 61.0
    return_after_idle = arb._may_take(b, True)
    assert (after_a_thought, return_after_idle) == (False, True)


def test_status_shows_the_phone_free_once_its_holders_process_has_exited():
    async def main():
        arb = Arbiter(idle_s=90, wait_s=5)
        gone = arb.client("pid:999999", "a script", 999999)  # no such process
        await call(arb, gone)
        return arb.describe()

    assert run(main()) == "free"

