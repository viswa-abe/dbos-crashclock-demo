#!/usr/bin/env python3
"""EXP-107 workload — mechanized red-pre-fix / green-post-fix demo of DBOS issue #640.

Bug (#640): ``BackgroundEventLoop.stop()`` can hang indefinitely during ``DBOS.destroy()``.
Pre-fix (``d7eb19f``) ``stop()`` does ``self._thread.join()`` with NO timeout; if a task on
the background loop swallows the ``CancelledError`` that ``_shutdown()`` sends (and keeps
running), ``asyncio.gather(...)`` inside ``_shutdown`` never returns, ``_loop.stop()`` is
never reached, ``run_forever()`` never exits, and the un-timed ``join()`` hangs FOREVER.
Fix (``fa727b9``, PR #647) changes the join to ``join(timeout=10.0)`` so ``stop()`` always
returns.

This workload exercises the EXACT patched code path: it loads the product's own
``dbos/_event_loop.py`` from the checked-out tree by file path (so PRE vs POST is decided
purely by which commit the wio image is pinned at — no vendored copy of the buggy code),
starts a real ``BackgroundEventLoop``, submits an uncancellable coroutine onto it, then
calls ``stop()`` under a watchdog with a declared terminal-state deadline.

Oracle (universal terminal-state): ``BackgroundEventLoop.stop()`` — the code the destroy
path calls — MUST reach a terminal state (return) within ``STOP_DEADLINE_S``. A hang past
the deadline ⇒ INVARIANT FAIL ⇒ RED. A watchdog thread guarantees the workload itself
always emits a verdict and exits (it never inherits the product's hang).

ALL fault timing derives from ``crashclock.offsets(seed, space)`` over a DECLARED space:
  * phase straddle  — WHERE, relative to the uncancellable coroutine becoming live on the
                      loop, ``stop()`` is called: ``in_flight`` (coroutine mid-await),
                      ``just_acked`` (just started its swallow-loop), ``settled`` (looping
                      a while). This is the race the bug hides behind: if ``stop()`` is
                      called before the coroutine is actually running on the loop, there is
                      nothing to swallow the cancel and stop() completes (GREEN even
                      pre-fix); once it is live and swallowing, pre-fix hangs.
  * latency window  — a sub-window stagger (ms) between "submit" and "call stop", swept
                      log-uniform so the sub-ms race boundary is densely covered.

Contract lines: ``CLOCK``, ``INVARIANT``, ``VERDICT`` (exit 0/1/3), VOID anti-vacuity
floors, ``ORACLE_SELFTEST`` (plants a never-returning join stub so the oracle MUST go RED).
"""

import importlib.util
import os
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import crashclock as cc  # vendored verbatim alongside this file  # noqa: E402

# --------------------------------------------------------------------------- #
# Declared timing space (the audited axis)
# --------------------------------------------------------------------------- #
CASE = "dbos640"
# Terminal-state deadline for stop(); well under the 900s guest ceiling. Overridable via
# env only to speed LOCAL iteration (the guest run always uses the 75s default) — the
# oracle semantics are identical, just a shorter watchdog.
STOP_DEADLINE_S = float(os.environ.get("STOP_DEADLINE_S", "75.0"))
PHASE = cc.phase_straddle("destroy_phase", settle_ms=250.0)
LAT = cc.latency_window("submit_stop_gap", window_ms=40.0)
MIN_LOOP_TICKS = 3            # anti-vacuity: loop must prove it is alive before we test

# --------------------------------------------------------------------------- #
# Load the product's own _event_loop.py from the checked-out tree (by path), so
# the PRE/POST behaviour is decided by the wio image commit, not by us.
# --------------------------------------------------------------------------- #

def _find_event_loop_path() -> str:
    # Env override wins (local repro points it at a specific extracted file).
    env = os.environ.get("EVENT_LOOP_PATH")
    if env and os.path.exists(env):
        return env
    # Fast path: the repo root is an ancestor of this workload file (.workers/workloads/*).
    # From HERE, the repo root is two dirs up; dbos/_event_loop.py sits at repo-root/dbos/.
    repo_root = os.path.dirname(os.path.dirname(HERE))  # .../<repo>/.workers/workloads -> <repo>
    for guess in (os.path.join(repo_root, "dbos", "_event_loop.py"),
                  os.path.join(os.getcwd(), "dbos", "_event_loop.py")):
        if os.path.exists(guess):
            return guess
    # Bounded search under sensible roots only (NEVER walk "/": on some hosts that hangs).
    candidates = []
    roots = [r for r in ("/workspace", repo_root, os.getcwd(), os.path.dirname(HERE))
             if r and os.path.isdir(r)]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "node_modules", "__pycache__", ".venv", "venv")]
            if "_event_loop.py" in filenames and os.path.basename(dirpath) == "dbos":
                candidates.append(os.path.join(dirpath, "_event_loop.py"))
        if candidates:
            break
    if not candidates:
        cc.void("could not locate dbos/_event_loop.py in the checked-out tree")
    candidates.sort(key=len)  # shortest = top-level package, not a vendored copy
    return candidates[0]


def _load_background_event_loop(path: str):
    """Import _event_loop.py as a standalone module, stubbing dbos._logger so the
    post-fix ``from ._logger import dbos_logger`` resolves without dragging in psycopg.
    Runs the EXACT pinned source of the file the fix touches."""
    # Provide a minimal fake 'dbos' package + 'dbos._logger' with a dbos_logger.
    import logging
    if "dbos" not in sys.modules:
        pkg = types.ModuleType("dbos")
        pkg.__path__ = []  # mark as package
        sys.modules["dbos"] = pkg
    logger_mod = types.ModuleType("dbos._logger")
    logger_mod.dbos_logger = logging.getLogger("dbos")
    sys.modules["dbos._logger"] = logger_mod

    spec = importlib.util.spec_from_file_location("dbos._event_loop", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dbos._event_loop"] = mod
    spec.loader.exec_module(mod)
    return mod.BackgroundEventLoop


# --------------------------------------------------------------------------- #
# Watchdog: guarantees the workload emits a verdict and exits even if stop() hangs.
# --------------------------------------------------------------------------- #

def run_stop_with_deadline(bg, deadline_s: float):
    """Call bg.stop() in a helper thread; return ('returned', elapsed) or ('hung', deadline).
    The helper thread is daemon, so if stop() hangs forever the process still exits."""
    done = threading.Event()
    result = {}

    def _call():
        t0 = time.monotonic()
        try:
            bg.stop()
            result["elapsed"] = time.monotonic() - t0
        except Exception as exc:  # pragma: no cover - stop() shouldn't raise
            result["exc"] = repr(exc)
        finally:
            done.set()

    th = threading.Thread(target=_call, daemon=True)
    t0 = time.monotonic()
    th.start()
    finished = done.wait(timeout=deadline_s)
    elapsed = time.monotonic() - t0
    if not finished:
        return "hung", elapsed
    if "exc" in result:
        return "raised", result["exc"]
    return "returned", result.get("elapsed", elapsed)


# --------------------------------------------------------------------------- #
# The uncancellable coroutine: swallows CancelledError, keeps running. This is the
# faithful analogue of a queued/scheduled coroutine on the background loop that does
# not honour cancellation during shutdown — the exact condition that makes pre-fix
# stop() hang. It counts ticks so anti-vacuity can confirm the loop was truly alive.
# --------------------------------------------------------------------------- #
_UNCANCELLABLE_SRC = None  # defined inline below


def make_uncancellable(state: dict):
    import asyncio

    async def _uncancellable():
        while True:
            try:
                await asyncio.sleep(0.01)
                state["ticks"] += 1
            except asyncio.CancelledError:
                # Swallow the cancel and keep going — this is what strands _shutdown().
                state["swallowed"] += 1
                # do NOT re-raise; loop forever
                continue
    return _uncancellable


def main() -> None:
    seed = cc.derive_seed()
    phase_pt = cc.offsets(seed, PHASE)
    lat_pt = cc.offsets(seed, LAT)
    # Combined armed point (one CLOCK line, both axes rendered).
    cc.clock_armed(CASE, {"phase": phase_pt["phase"], "settle_ms": phase_pt["settle_ms"],
                          "gap_ms": lat_pt["T_ms"], "window_ms": lat_pt["window_ms"],
                          "deadline_s": STOP_DEADLINE_S})

    el_path = _find_event_loop_path()
    cc.log(f"event_loop source: {el_path}")
    # Provenance: does this tree have the timeout fix? (audit line, not used by oracle)
    try:
        src = open(el_path).read()
        has_timeout = "join(timeout" in src.replace(" ", "")
        cc.log(f"tree has_timeout_fix={has_timeout}")
    except Exception:
        pass

    BackgroundEventLoop = _load_background_event_loop(el_path)

    # ---- ORACLE_SELFTEST: plant a hang so the deadline oracle MUST fire RED. ----
    if cc.selftest_active():
        cc.log("ORACLE_SELFTEST: planting a never-returning stop() (join stub that never joins)")

        class _HangingLoop:
            def stop(self):
                # Never returns — models the pre-fix un-timed join on a stranded loop.
                while True:
                    time.sleep(1.0)
        bg = _HangingLoop()
        outcome, info = run_stop_with_deadline(bg, STOP_DEADLINE_S)
        if outcome == "hung":
            cc.red(f"SELFTEST planted hang not caught? stop() ran {info:.1f}s past deadline "
                   f"{STOP_DEADLINE_S}s", inv=("stop_terminal", "stop-reaches-terminal-state"))
        # (red() exits 1). If we ever reach here the selftest FAILED to plant a hang.
        cc.void("SELFTEST stop() returned — planted hang did not take effect")

    # ---- Real case: start the loop, submit the uncancellable coroutine, then stop(). ----
    state = {"ticks": 0, "swallowed": 0}
    bg = BackgroundEventLoop()
    bg.start()

    # Submit the uncancellable coroutine onto the background loop (no-wait, like a queued
    # scheduled workflow). We schedule it directly via run_coroutine_threadsafe on the
    # loop the product created (bg._loop) — the same mechanism submit_coroutine uses — so
    # this works identically at both pins (submit_coroutine_nowait didn't exist at d7eb19f).
    import asyncio
    if getattr(bg, "_loop", None) is None:
        cc.void("background loop not started (bg._loop is None) — cannot submit coroutine")
    no_fault = bool(os.environ.get("NO_FAULT"))
    if no_fault:
        # No-fault baseline: submit a WELL-BEHAVED coroutine that honours cancellation.
        # stop() must complete promptly even PRE-fix — proves the oracle is not vacuous
        # (it stays GREEN when no strand exists), so a PRE-fix RED is a real finding.
        cc.log("NO_FAULT: submitting a cancellation-honouring coroutine (no strand)")

        async def _wellbehaved():
            while True:
                await asyncio.sleep(0.01)
                state["ticks"] += 1
        asyncio.run_coroutine_threadsafe(_wellbehaved(), bg._loop)
    else:
        coro = make_uncancellable(state)()
        asyncio.run_coroutine_threadsafe(coro, bg._loop)

    # Wait until the loop proves it is alive (anti-vacuity floor): the coroutine must have
    # ticked at least MIN_LOOP_TICKS times, else the kill point was never reached.
    warm_deadline = time.monotonic() + 10.0
    while state["ticks"] < MIN_LOOP_TICKS and time.monotonic() < warm_deadline:
        time.sleep(0.005)
    if state["ticks"] < MIN_LOOP_TICKS:
        cc.void(f"background loop never reached {MIN_LOOP_TICKS} ticks "
                f"(ticks={state['ticks']}) — coroutine not live, kill point unreached")

    # Phase straddle: WHERE, relative to the coroutine being live, we call stop().
    #   in_flight   — call immediately (coroutine mid-await sleep)
    #   just_acked  — tiny settle so it is firmly in its await/except cycle
    #   settled     — wait settle_ms so it has been swallowing/looping a while
    phase = phase_pt["phase"]
    gap_ms = lat_pt["T_ms"]
    settle_ms = phase_pt["settle_ms"]
    if phase == "settled":
        time.sleep(settle_ms / 1000.0)
    elif phase == "just_acked":
        time.sleep(0.005)
    # in_flight: no extra wait beyond the warm-up.
    # Sub-window stagger from the latency axis (log-uniform, sub-ms dense).
    if gap_ms > 0:
        time.sleep(gap_ms / 1000.0)

    ticks_before = state["ticks"]
    outcome, info = run_stop_with_deadline(bg, STOP_DEADLINE_S)

    summary = (f"phase={phase} gap_ms={gap_ms:.3g} ticks={ticks_before} "
               f"swallowed={state['swallowed']} outcome={outcome}")

    if outcome == "hung":
        cc.red(f"stop() HUNG past {STOP_DEADLINE_S:.0f}s deadline during shutdown "
               f"({summary}) — #640 un-timed join never returns",
               inv=("stop_terminal", "stop-reaches-terminal-state"))
    if outcome == "raised":
        cc.red(f"stop() raised {info} ({summary})",
               inv=("stop_terminal", "stop-reaches-terminal-state"))

    # returned within deadline — GREEN (post-fix, or a phase where the race didn't strand)
    cc.invariant("stop_terminal", "stop-reaches-terminal-state", True,
                 f"stop() returned in {info:.2f}s (< {STOP_DEADLINE_S:.0f}s deadline); {summary}")
    cc.green(f"BackgroundEventLoop.stop() reached terminal state; {summary}")


if __name__ == "__main__":
    main()
