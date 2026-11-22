import asyncio
import time

import pytest

from phonectl import arbiter as arbiter_module
from phonectl.arbiter import Abandoned, Arbiter, StillWaiting


def run(coro):
    return asyncio.run(coro)


async def call(arb, client, seconds=0.0, log=None):
    """One tool call by client that takes `seconds` while holding the phone."""
    async with arb.use(client) as lease:
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
        arb = Arbiter(idle_s=10, wait_s=0.2, keep_place_s=5)
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


def test_the_holder_is_judged_by_its_own_pace():
    # A quick holder that has stopped is done long before the slowest pace's grace runs out.
    async def main():
        arb = Arbiter(idle_s=10, wait_s=10, grace_min_s=0.05, grace_start_s=10)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        for _ in range(5):
            await call(arb, a)
            await asyncio.sleep(0.03)
        assert arb.grace() < 0.5
        t0 = time.monotonic()
        await call(arb, b)
        return time.monotonic() - t0

    assert run(main()) < 1.0


def test_a_new_holder_starts_from_the_default_grace():
    async def main():
        arb = Arbiter(idle_s=10, wait_s=10, grace_min_s=0.05, grace_start_s=3)
        a, b = arb.client("a", "A"), arb.client("b", "B")
        for _ in range(4):
            await call(arb, a)
            await asyncio.sleep(0.02)
        assert await arb.release(a)
        await call(arb, b)  # b's pace is not a's
        return arb.grace()

    assert run(main()) == 3


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
        arb = Arbiter(idle_s=10, wait_s=5, keep_place_s=5)
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
