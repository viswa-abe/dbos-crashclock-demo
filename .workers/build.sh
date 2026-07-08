#!/bin/sh
set -eu
#
# EXP-104 crash-clock demo build (DBOS #716) — self-contained, no system Postgres.
#
# The wio guest has NO system PG binaries (the existing dbos-workload run-with-postgres.sh
# setup-blocks when they are absent). We vendor an embedded Postgres via the `pgserver`
# wheel (bundles a full PostgreSQL that runs from Python over a unix socket, no root, no
# initdb on PATH) and install DBOS FROM THE REPO TREE ITSELF (pip install the checkout),
# so the image is pinned to this branch's commit — pre vs post is the ONLY variable.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${ROOT}/.workers/vendor/venv"
TMP="${ROOT}/.workers/tmp"
mkdir -p "${ROOT}/.workers/vendor" "${TMP}"

# Mirror dbos-workload/build.sh log-capture wrapper so a failing prepare surfaces logs.
if [ "${WIO_BUILD_LOG_CAPTURED:-0}" != "1" ]; then
  OUT="${TMP}/build.stdout.log"; ERR="${TMP}/build.stderr.log"
  rm -f "${OUT}" "${ERR}"
  if WIO_BUILD_LOG_CAPTURED=1 sh "$0" "$@" >"${OUT}" 2>"${ERR}"; then
    cat "${OUT}"; cat "${ERR}" >&2; exit 0
  else
    status=$?; cat "${OUT}" >&2; cat "${ERR}" >&2; exit "${status}"
  fi
fi

# --- Python bootstrap (system python3 if it can venv+ensurepip, else uv) ------------
UV_BIN=""
ensure_python() {
  if command -v python3 >/dev/null 2>&1 && python3 - <<'PY' >/dev/null 2>&1; then
import ensurepip, venv
PY
    PYTHON_BOOTSTRAP="python3"; return
  fi
  UV_VERSION="${UV_VERSION:-0.7.13}"
  case "$(uname -m)" in
    x86_64|amd64) UV_ARCH="x86_64-unknown-linux-gnu" ;;
    aarch64|arm64) UV_ARCH="aarch64-unknown-linux-gnu" ;;
    *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
  esac
  curl -fsSL --retry 3 -o "${TMP}/uv.tar.gz" \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_ARCH}.tar.gz"
  tar -C "${TMP}" -xzf "${TMP}/uv.tar.gz"
  UV_BIN="${TMP}/uv-${UV_ARCH}/uv"
  export UV_CACHE_DIR="${TMP}/uv-cache" UV_PYTHON_INSTALL_DIR="${TMP}/python" UV_PYTHON_DOWNLOADS=true
  "${UV_BIN}" python install 3.12
  PYTHON_BOOTSTRAP="$("${UV_BIN}" python find 3.12)"
}

ensure_python
rm -rf "${VENV}"
if [ -n "${UV_BIN}" ]; then
  "${UV_BIN}" venv --seed --python "${PYTHON_BOOTSTRAP}" "${VENV}"
else
  "${PYTHON_BOOTSTRAP}" -m venv "${VENV}"
fi

PY="${VENV}/bin/python"
"${PY}" -m pip install --upgrade pip

# DBOS from THIS repo tree (pin-keyed). PDM SCM version needs a value off a shallow tree.
export PDM_BUILD_SCM_VERSION="${PDM_BUILD_SCM_VERSION:-0.0.0+crashclock}"
"${PY}" -m pip install "${ROOT}"

# Embedded Postgres (bundled server binaries in the wheel) + driver DBOS uses.
"${PY}" -m pip install "pgserver>=0.1.4" "psycopg[binary]>=3.1" "sqlalchemy>=2.0"

# Install the uuid-ossp marker extension into the bundled PG (DBOS's migration needs it;
# it actually uses gen_random_uuid()). Persisted in the image so no runtime patching races.
"${PY}" - <<'PY'
from pgserver._commands import POSTGRES_BIN_PATH
ext = POSTGRES_BIN_PATH.parent / "share" / "postgresql" / "extension"
if not ext.exists():
    ext = POSTGRES_BIN_PATH.parent / "share" / "extension"
(ext / "uuid-ossp.control").write_text(
    "comment = 'WIO compatibility uuid-ossp marker'\ndefault_version = '1.0'\n"
    "relocatable = true\ntrusted = true\n")
(ext / "uuid-ossp--1.0.sql").write_text(
    "-- DBOS uses built-in gen_random_uuid(); marker satisfies CREATE EXTENSION.\n")
print("installed uuid-ossp marker into", ext)
PY

# Smoke: import DBOS from the installed tree, boot embedded PG, run the DBOS migration once.
"${PY}" - <<'PY'
import dbos, pgserver, tempfile, pathlib
print("prepared dbos from repo tree:", dbos.__file__)
d = pathlib.Path(tempfile.mkdtemp()) / "pg"
d.mkdir(parents=True)
srv = pgserver.get_server(d, cleanup_mode="delete")
url = srv.get_uri(database="postgres")
print("prepared embedded postgres:", url)
from dbos import DBOS, DBOSConfig
DBOS(config={"name": "ccbuild", "system_database_url": url, "database_url": url})
DBOS.launch(); DBOS.destroy()
srv.cleanup()
print("embedded postgres + DBOS migration OK")
PY

echo "build.sh: crash-clock demo image prepared (dbos @ repo pin + embedded pgserver)"
