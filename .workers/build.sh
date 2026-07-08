#!/bin/sh
set -eu
#
# EXP-107 crash-clock demo build (DBOS #640) — minimal, SQLite/pure-Python, no Postgres.
#
# The #640 workload does NOT import the `dbos` package. It loads the product's own
# `dbos/_event_loop.py` (the file the fix touches) directly by file path, stubbing
# `dbos._logger` in-process. So this build needs nothing but a working python3 and the
# checked-out repo tree — no pip install of DBOS, no psycopg/pgserver glibc wheels (which
# fail to import on the musl guest per goal.md §2 runtime facts).
#
# A tiny pure-Python `psycopg` shim is installed ONLY as a safety net, so that if anything
# in the guest transitively imports psycopg it resolves without the glibc binary wheel.
# It is never exercised by this workload (SQLite path only / no DBOS package import).

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="${ROOT}/.workers/tmp"
mkdir -p "${TMP}"

# Log-capture wrapper (mirror dbos-workload/build.sh so a failing prepare surfaces logs).
if [ "${WIO_BUILD_LOG_CAPTURED:-0}" != "1" ]; then
  OUT="${TMP}/build.stdout.log"; ERR="${TMP}/build.stderr.log"
  rm -f "${OUT}" "${ERR}"
  if WIO_BUILD_LOG_CAPTURED=1 sh "$0" "$@" >"${OUT}" 2>"${ERR}"; then
    cat "${OUT}"; cat "${ERR}" >&2; exit 0
  else
    status=$?; cat "${OUT}" >&2; cat "${ERR}" >&2; exit "${status}"
  fi
fi

echo "build.sh: python3 = $(command -v python3 || echo MISSING)"
python3 --version
python3 -c "import asyncio, threading, importlib.util; print('stdlib OK')"

# Pure-Python psycopg shim (safety net; never used by the #640 workload). Placed on a
# site dir that python3 already imports. We install it next to the workloads so the
# workload's own sys.path.insert(0, HERE) makes it importable if ever needed.
SHIM_DIR="${ROOT}/.workers/vendor/site"
mkdir -p "${SHIM_DIR}/psycopg"
cat > "${SHIM_DIR}/psycopg/__init__.py" <<'PY'
"""Minimal pure-Python psycopg shim (EXP-107). Only exists so an accidental
`import psycopg` resolves on musl where the real binary wheel won't import. The #640
workload never touches Postgres — it drives dbos/_event_loop.py by file path."""
class Error(Exception): pass
class OperationalError(Error): pass
class errors:  # namespace some code paths reference
    class SerializationFailure(Error): pass
    class DeadlockDetected(Error): pass
def connect(*a, **k):
    raise OperationalError("psycopg shim: no real Postgres in EXP-107 (SQLite/no-DB path)")
__version__ = "0.0.0+exp107shim"
PY
echo "PSYCOPG_SHIM=${SHIM_DIR}" > "${TMP}/build.env"

# Verify the workload can locate and load dbos/_event_loop.py from THIS tree.
python3 - "$ROOT" <<'PY'
import sys, os, importlib.util, types, logging
root = sys.argv[1]
elp = os.path.join(root, "dbos", "_event_loop.py")
assert os.path.exists(elp), f"missing {elp}"
# stub dbos._logger, then load _event_loop by path (proves the workload's load path works)
pkg = types.ModuleType("dbos"); pkg.__path__ = []; sys.modules["dbos"] = pkg
lm = types.ModuleType("dbos._logger"); lm.dbos_logger = logging.getLogger("dbos")
sys.modules["dbos._logger"] = lm
spec = importlib.util.spec_from_file_location("dbos._event_loop", elp)
m = importlib.util.module_from_spec(spec); sys.modules["dbos._event_loop"] = m
spec.loader.exec_module(m)
has_timeout = "join(timeout" in open(elp).read().replace(" ", "")
print(f"build.sh: loaded BackgroundEventLoop from {elp} (has_timeout_fix={has_timeout})")
PY

echo "build.sh: EXP-107 #640 crash-clock demo image prepared (no DBOS install; stdlib + file-path load)"
