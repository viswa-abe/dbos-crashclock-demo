#!/usr/bin/env python3
"""DBOS #716 demo workload: Postgres restart DURING recovery strands PENDING workflows.

Bug (dbos-inc/dbos-transact-py #716, fixed by PR #717 / 2b31ab8d):
  On startup DBOS recovers pending/enqueued workflows. The recovery path calls
  `SystemDatabase.get_pending_workflows` and (for queued workflows)
  `clear_queue_assignment`. At the PRE pin neither is wrapped in `@db_retry()`, so a
  TRANSIENT database failure (Postgres bouncing) during recovery raises an
  OperationalError that either aborts `DBOS.launch()` outright OR is swallowed by
  `startup_recovery_thread` which then *drops the workflow from the retry list*
  (`pending_workflows.remove(...)`). Either way the workflow is stranded PENDING forever.
  PR #717 adds `@db_retry()` so the transient failure is retried internally and recovery
  completes — every workflow reaches a terminal state.

Crash-clock (lib crashclock): `--depth N` sweeps WHEN the Postgres restart lands relative
to the start of recovery — a latency-window clock (log-uniform 0..window ms) with a phase
straddle (restart before the app launches / just as recovery begins / after settle). The
seed picks the point; the SPACE is declared here so an auditor sees the axis.

Oracle (terminal-state, universal): every workflow this run ENQUEUED must reach a terminal
state (SUCCESS or ERROR) within the deadline. A workflow still PENDING/ENQUEUED at the
deadline is a STRAND => INVARIANT FAIL (RED). Anti-vacuity: the PG restart must have landed
during the recovery window (a real transient seen by the app) and at least MIN_ENQUEUED
workflows must have been enqueued, else VOID.

Exit codes: 0 green, 1 red (strand found), 3 void/blocked.
"""

import os
import sys
import time
import signal
import tempfile
import textwrap
import subprocess
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import crashclock as cc  # noqa: E402

CASE = "dbos716"
N_WORKFLOWS = int(os.environ.get("CC_N_WORKFLOWS", "24"))
MIN_ENQUEUED = 6
DEADLINE_S = float(os.environ.get("CC_DEADLINE_S", "90"))
QUEUE_NAME = "cc_recovery_q"
WORK_DIR = Path(os.environ.get("CC_WORK_DIR", "/tmp/cc716"))

# The app module, run in TWO roles:
#   role=seed  — launch, enqueue N blocking workflows, wait until they are PENDING+owned
#                (dequeued and executing), print READY, then block so we can SIGKILL it
#                mid-flight (leaving durable PENDING workflows for the next startup).
#   role=recover — launch again against the same DB; DBOS.launch() runs
#                startup_recovery_thread over those PENDING workflows (the #716 path);
#                the workflows unblock (DBOS.recv times out fast on recovery) and finish.
APP_SRC = textwrap.dedent(
    '''
    import os, sys, time
    from dbos import DBOS, DBOSConfig, Queue

    DB_URL = os.environ["CC_DB_URL"]
    QUEUE_NAME = os.environ["CC_QUEUE_NAME"]
    N = int(os.environ["CC_N_WORKFLOWS"])
    SEED = os.environ["CC_SEED"]
    ROLE = sys.argv[1] if len(sys.argv) > 1 else "recover"
    # On the seed run the workflows block for a long time (kept PENDING for the SIGKILL).
    # On recovery the same workflow re-executes; its recv step is durable so it returns
    # quickly once recovered — a short recovery-side timeout keeps the demo bounded.
    BLOCK_S = 600.0 if ROLE == "seed" else float(os.environ.get("CC_RECOVER_BLOCK_S", "0.5"))

    config: DBOSConfig = {"name": "cc716app", "system_database_url": DB_URL,
                          "database_url": DB_URL}
    DBOS(config=config)
    queue = Queue(QUEUE_NAME)

    @DBOS.workflow()
    def recovered_wf(n: int) -> int:
        # A single non-durable step that sleeps: on the seed run it sleeps a long time and
        # is SIGKILLed mid-step (leaving the workflow PENDING). On recovery the workflow is
        # re-executed from the start; the step re-runs but only sleeps a short time, so the
        # workflow reaches SUCCESS quickly once recovery re-dispatches it.
        @DBOS.step()
        def do_work(k: int) -> int:
            time.sleep(BLOCK_S)
            return k * 2
        return do_work(n)

    if __name__ == "__main__":
        DBOS.launch()   # recovery role: this triggers startup_recovery_thread (the #716 path)
        print("APP_LAUNCHED", flush=True)
        if ROLE == "seed":
            handles = [queue.enqueue(recovered_wf, i) for i in range(N)]
            # wait until every workflow has been dequeued and is PENDING (executor-owned)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                sts = [DBOS.get_workflow_status(h.workflow_id) for h in handles]
                pend = sum(1 for s in sts if s and s.status == "PENDING")
                if pend >= N:
                    break
                time.sleep(0.05)
            print("READY_PENDING", flush=True)
        # stay alive; the driver decides when to kill / when the run ends
        ttl = time.monotonic() + float(os.environ.get("CC_APP_TTL_S", "60"))
        while time.monotonic() < ttl:
            time.sleep(0.2)
    '''
)


PG_BIN = os.environ.get("CC_PG_BIN", "/usr/bin")
PG_PORT = int(os.environ.get("CC_PG_PORT", "5432"))
PG_HOST = "127.0.0.1"


def ensure_uuid_ossp_stub() -> None:
    """DBOS's migration runs CREATE EXTENSION "uuid-ossp", which the guest's system
    PostgreSQL may not ship (DBOS actually uses the built-in gen_random_uuid()). Install a
    no-op marker so CREATE EXTENSION succeeds — same trick as the corpus's
    run-with-postgres.sh. Idempotent."""
    try:
        share = subprocess.run([f"{PG_BIN}/pg_config", "--sharedir"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        share = "/usr/share/postgresql16"
    ext_dir = Path(share) / "extension"
    ctrl = ext_dir / "uuid-ossp.control"
    if ctrl.exists() or not ext_dir.exists():
        return
    try:
        ctrl.write_text(
            "comment = 'WIO compatibility uuid-ossp marker'\n"
            "default_version = '1.0'\nrelocatable = true\ntrusted = true\n")
        (ext_dir / "uuid-ossp--1.0.sql").write_text(
            "-- DBOS uses built-in gen_random_uuid(); marker satisfies CREATE EXTENSION.\n")
    except PermissionError:
        pass  # dir not writable; DBOS migration may still succeed if ext already present


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


class SystemPgHandle(cc.DependencyHandle):
    """The guest's SYSTEM PostgreSQL 16 (musl-native /usr/bin binaries) as a crash-clock
    dependency — the same approach as the corpus run-with-postgres.sh. The guest runs as
    root, so the server itself runs as the unprivileged 'postgres' user.

    stop()  = pg_ctl -m immediate stop (crash-like; drops live connections -> the transient
              OperationalError the recovery path must survive).
    start() = pg_ctl start (bring it back).
    """

    def __init__(self, pgdata: Path):
        ensure_uuid_ossp_stub()
        self.pgdata = pgdata
        self._as_postgres = (os.getuid() == 0)
        if self.pgdata.exists():
            import shutil
            shutil.rmtree(self.pgdata, ignore_errors=True)
        self.pgdata.mkdir(parents=True, exist_ok=True)
        if self._as_postgres:
            _run(["chown", "-R", "postgres:postgres", str(self.pgdata)])
        self._initdb()
        self.start()

    def _sh(self, shell_cmd: str, timeout=60):
        """Run a pg command, as the postgres user when we are root."""
        if self._as_postgres:
            return _run(["su", "postgres", "-c", shell_cmd], timeout=timeout)
        return _run(["sh", "-c", shell_cmd], timeout=timeout)

    def _initdb(self):
        # -U postgres so the superuser role is 'postgres' regardless of the OS user that
        # runs initdb (root->su postgres in the guest; the invoking user locally).
        # --no-sync: skip fsync during initdb — the DB is ephemeral per case, and TCG
        # single-thread emulation makes syncing initdb pathologically slow (90s+).
        r = self._sh(f"{PG_BIN}/initdb -D '{self.pgdata}' -A trust -U postgres "
                     f"--encoding=UTF8 --no-locale --no-sync", timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"initdb failed: {r.stderr[-400:]}")

    def uri(self) -> str:
        return f"postgresql://postgres@{PG_HOST}:{PG_PORT}/postgres"

    def start(self) -> None:
        log = self.pgdata / "server.log"
        # fsync=off / synchronous_commit=off: ephemeral per-case DB on slow TCG emulation;
        # durability across a HOST crash is irrelevant (we only crash PG, not the guest).
        opts = (f"-h '{PG_HOST}' -p {PG_PORT} -k /tmp -c fsync=off "
                f"-c synchronous_commit=off -c full_page_writes=off")
        r = self._sh(f"{PG_BIN}/pg_ctl -D '{self.pgdata}' -l '{log}' -o \"{opts}\" -w start",
                     timeout=60)
        # -w waits for ready; if it returns nonzero, poll pg_isready as a fallback
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _run([f"{PG_BIN}/pg_isready", "-h", PG_HOST, "-p", str(PG_PORT),
                     "-U", "postgres"]).returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError(f"postgres did not become ready: {r.stderr[-300:]}")

    def stop(self) -> None:
        self._sh(f"{PG_BIN}/pg_ctl -D '{self.pgdata}' -m immediate stop", timeout=20)
        # wait until it is actually down
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _run([f"{PG_BIN}/pg_isready", "-h", PG_HOST, "-p", str(PG_PORT),
                     "-U", "postgres"]).returncode != 0:
                return
            time.sleep(0.05)

    def is_up(self) -> bool:
        return _run([f"{PG_BIN}/pg_isready", "-h", PG_HOST, "-p", str(PG_PORT),
                     "-U", "postgres"]).returncode == 0

    def cleanup(self) -> None:
        try:
            self.stop()
        except Exception:
            pass


def pending_ids(db_url: str) -> list:
    """Workflow ids currently non-terminal (PENDING or ENQUEUED) for our queue."""
    from dbos import DBOSClient

    client = DBOSClient(system_database_url=db_url)
    wfs = client.list_workflows(status=["PENDING", "ENQUEUED"], queue_name=QUEUE_NAME,
                                load_input=False, load_output=False)
    return [w.workflow_id for w in wfs]


def statuses(db_url: str, ids: list) -> dict:
    """Map workflow_id -> status string, read directly from the system DB."""
    from dbos import DBOSClient

    client = DBOSClient(system_database_url=db_url)
    out = {}
    try:
        wfs = client.list_workflows(workflow_ids=ids, load_input=False, load_output=False)
        for w in wfs:
            out[w.workflow_id] = w.status
    except Exception as exc:
        for wid in ids:
            out[wid] = f"ERR:{type(exc).__name__}"
    for wid in ids:
        out.setdefault(wid, "MISSING")
    return out


def main():
    seed = cc.derive_seed()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    pgdata = WORK_DIR / f"pg-{seed}"

    # --- Declared crash-clock space: WHEN the PG restart lands during recovery ---------
    # latency-window: 0..window ms after the recovering app is launched (log-uniform, with
    # a zero-corner = restart right as launch begins). phase straddle chooses whether the
    # restart brackets the launch (in_flight), lands just after recovery starts
    # (just_acked), or after a settle pad (settled).
    window_space = cc.latency_window("recovery_restart", window_ms=800.0)
    phase_space = cc.phase_straddle("recovery_phase", settle_ms=400.0)
    # PG down-window swept 30..300ms: long enough to make the recovery-lookup calls
    # (get_pending_workflows / clear_queue_assignment) see a transient, short enough that a
    # hardened (db_retry) build recovers and workflow bodies still complete.
    down_space = cc.latency_window("pg_down", window_ms=300.0, floor_ms=30.0)

    pt_when = cc.offsets(seed, window_space)
    pt_phase = cc.offsets(seed, phase_space)
    pt_down = cc.offsets(seed, down_space)
    restart_delay_s = pt_when["T_ms"] / 1000.0
    phase = pt_phase["phase"]
    down_s = pt_down["T_ms"] / 1000.0
    cc.clock_armed(CASE, {**pt_when, "phase": phase, "down_ms": pt_down["T_ms"]})

    # --- Boot the guest's system Postgres ----------------------------------------------
    try:
        pg = SystemPgHandle(pgdata)
    except Exception as exc:
        cc.void(f"system postgres failed to boot: {type(exc).__name__}: {exc}")
    db_url = pg.uri()
    cc.log(f"system pg up: {db_url}")

    # --- App module used for both the seed run and the recovery run -------------------
    app_path = WORK_DIR / f"app-{seed}.py"
    app_path.write_text(APP_SRC)
    base_env = {**os.environ, "CC_DB_URL": db_url, "CC_QUEUE_NAME": QUEUE_NAME,
                "CC_SEED": str(seed), "CC_N_WORKFLOWS": str(N_WORKFLOWS)}

    # --- Phase 1: seed run — enqueue N blocking workflows, let them become PENDING, then
    #     SIGKILL the app mid-flight so they are stranded PENDING for the next startup. --
    seed_env = {**base_env, "CC_APP_TTL_S": "600"}
    app1 = subprocess.Popen([sys.executable, str(app_path), "seed"], env=seed_env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ready = False
    t0 = time.monotonic()
    seed_lines = []
    while time.monotonic() - t0 < 90:
        if app1.poll() is not None:
            break
        line = app1.stdout.readline()
        if line:
            seed_lines.append(line.strip())
            if "READY_PENDING" in line:
                ready = True
                break
    if not ready:
        cc.kill_self_child(app1, mode="sigkill"); app1.wait(timeout=10); pg.cleanup()
        cc.void(f"seed app never reached READY_PENDING: {seed_lines[-6:]}")

    # discover the actual workflow ids that are PENDING (DBOS assigns uuids)
    ids = pending_ids(db_url)
    pending_now = len(ids)
    if pending_now < MIN_ENQUEUED:
        cc.kill_self_child(app1, mode="sigkill"); app1.wait(timeout=10); pg.cleanup()
        cc.void(f"only {pending_now} workflows PENDING before kill — below floor {MIN_ENQUEUED}")
    cc.log(f"seed app READY: {pending_now} workflows PENDING (executor-owned)")

    # SIGKILL app1 mid-flight (the crash that leaves durable PENDING workflows)
    cc.kill_self_child(app1, mode="sigkill")
    app1.wait(timeout=10)
    cc.log(f"SIGKILLed seed app — {pending_now} workflows stranded PENDING for recovery")

    # --- Phase 2: recovery run — launch app2; its DBOS.launch() runs the startup
    #     recovery thread over those PENDING workflows. Restart PG at the seed-swept clock
    #     point DURING that recovery. The phase straddle chooses WHEN inside the window: --
    #       in_flight  — the instant recovery is observed to have begun (tightest race)
    #       just_acked — a seed-swept few-ms after recovery begins
    #       settled    — after a settle pad, deeper into the recovery drain
    rec_env = {**base_env, "CC_APP_TTL_S": str(int(DEADLINE_S))}
    app = subprocess.Popen([sys.executable, str(app_path), "recover"], env=rec_env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # Wait until recovery has begun: any workflow moved off its initial PENDING state (the
    # recovery thread is mid-loop). Poll fast so the transient lands INSIDE the clear loop,
    # not after it. Bounded.
    recovery_started = False
    rstart_deadline = time.monotonic() + 40
    while time.monotonic() < rstart_deadline:
        if app.poll() is not None:
            break
        snap = statuses(db_url, ids)
        if any(s != "PENDING" for s in snap.values()):
            recovery_started = True
            break
        time.sleep(0.002)

    if phase == "in_flight":
        delay = 0.0
    elif phase == "just_acked":
        delay = restart_delay_s
    else:  # settled
        delay = restart_delay_s + pt_phase["settle_ms"] / 1000.0
    cc.log(f"phase={phase}: recovery_started={recovery_started}; restart PG at "
           f"+{delay:.3f}s into recovery (down {down_s:.2f}s)")
    time.sleep(delay)

    landed_during_recovery = recovery_started and app.poll() is None
    cc.restart_dependency(pg, down_dur_s=down_s)

    # --- Wait for terminal states up to the deadline ----------------------------------
    deadline = time.monotonic() + DEADLINE_S
    final = {}
    while time.monotonic() < deadline:
        final = statuses(db_url, ids)
        if all(s in ("SUCCESS", "ERROR") for s in final.values()):
            break
        time.sleep(1.0)

    # collect app output for evidence
    try:
        app.send_signal(signal.SIGTERM)
        out, _ = app.communicate(timeout=15)
    except Exception:
        app.kill()
        out = ""
    for line in (out or "").splitlines():
        if "Recovering" in line or "APP_LAUNCHED" in line or "OperationalError" in line:
            cc.log(f"app| {line}")

    stranded = [w for w, s in final.items() if s in ("ENQUEUED", "PENDING")]
    terminal = [w for w, s in final.items() if s in ("SUCCESS", "ERROR")]
    cc.log(f"final statuses: terminal={len(terminal)} stranded={len(stranded)} "
           f"detail={ {w: final[w] for w in stranded[:6]} }")

    # anti-vacuity: the restart has to have plausibly hit the recovery window
    if not landed_during_recovery:
        pg.cleanup()
        cc.void("PG restart did not land during recovery (app already exited) — vacuous")

    if cc.selftest_active():
        # plant a strand the oracle MUST catch, independent of the product
        if terminal:
            stranded = stranded + [terminal[0]]
        cc.log(f"ORACLE_SELFTEST: planted a strand ({stranded[-1] if stranded else 'none'})")

    if stranded:
        pg.cleanup()
        cc.red(
            f"{len(stranded)} workflow(s) stranded non-terminal after PG restart during "
            f"recovery: {stranded[:8]} (statuses {[final[w] for w in stranded[:8]]})",
            inv=("terminal_state", "every-accepted-workflow-terminal"),
        )

    cc.invariant("terminal_state", "every-accepted-workflow-terminal", True,
                 f"all {len(ids)} enqueued workflows reached a terminal state within "
                 f"{DEADLINE_S:.0f}s despite a PG restart during recovery "
                 f"(clock T={pt_when['T_ms']:.1f}ms phase={phase})")
    pg.cleanup()
    cc.green(f"{len(ids)}/{len(ids)} workflows terminal after mid-recovery PG restart")


if __name__ == "__main__":
    main()
