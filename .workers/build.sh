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
PIP="${PY} -m pip install --no-cache-dir --retries 5 --timeout 120"

# DBOS from THIS repo tree (pin-keyed). PDM SCM version needs a value off a shallow tree.
export PDM_BUILD_SCM_VERSION="${PDM_BUILD_SCM_VERSION:-0.0.0+crashclock}"
${PIP} "${ROOT}"

# DB drivers DBOS uses at runtime. The guest already ships musl-native system PostgreSQL
# 16 (/usr/bin/initdb|postgres|pg_ctl) — the workload drives THAT (run-with-postgres.sh
# style), so no embedded-PG wheel is needed (pgserver's glibc binaries won't run on musl).
${PIP} "psycopg[binary]>=3.1" "sqlalchemy>=2.0"

# Verify the DBOS import from the installed tree is real (hard requirement).
"${PY}" - <<'PY'
import dbos
print("prepared dbos from repo tree:", dbos.__file__)
PY

echo "build.sh: crash-clock demo image prepared (dbos @ repo pin; guest system Postgres)"
