#!/usr/bin/env bash
#
# One-time client setup for a fresh box (e.g. an AWS instance).
#
# Creates a venv with the read clients (requirements.txt) AND builds the source-only
# QuestDB 5.0 client (QWP + Arrow egress) that the questdb load/read scripts import -
# it is NOT on PyPI (pip install questdb gives ILP-only 4.x).
#
# Prereqs on the host (install before running):
#   - Python 3.12 (or 3.13)         python3.12 + python3.12-venv
#   - a Rust toolchain (cargo)       curl https://sh.rustup.rs -sSf | sh -s -- -y
#   - a C compiler + git             build-essential git curl
#   Ubuntu one-liner:
#     sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv build-essential git curl
#     curl https://sh.rustup.rs -sSf | sh -s -- -y && . "$HOME/.cargo/env"
#
# After this: PYTHON=./.venv/bin/python ./bench.sh 50000000
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY_BIN="${PY_BIN:-python3.12}"
VENV="${VENV:-$HERE/.venv}"
BUILD_DIR="${BUILD_DIR:-$HERE/.pyqdb_build}"
# Hard-pinned commit (same as Dockerfile.client). Bump when the client moves; drop
# this whole from-source dance once the 5.0 client ships on PyPI.
PYQDB_REF="${PYQDB_REF:-deb3c21e39ffbf5008410379900c63450fdf1b1d}"
PYQDB_REPO="${PYQDB_REPO:-https://github.com/questdb/py-questdb-client}"

echo "==> Creating venv at $VENV"
"$PY_BIN" -m venv "$VENV"
"$VENV/bin/pip" install -U pip "cython>=3.1.2" "setuptools>=80.9.0" numpy
"$VENV/bin/pip" install -r "$HERE/requirements.txt"

echo "==> Building QuestDB 5.0 client from $PYQDB_REPO@$PYQDB_REF"
if [ ! -d "$BUILD_DIR/.git" ]; then
  git clone "$PYQDB_REPO" "$BUILD_DIR"
fi
cd "$BUILD_DIR"
git fetch --all --tags
git checkout "$PYQDB_REF"
# Only the direct c-questdb-client submodule is needed to build; not the whole
# QuestDB monorepo that a recursive init would drag in.
git submodule update --init --depth 1
# QUESTDB_INSECURE_SKIP_VERIFY=1 enables tls_verify=unsafe_off (self-signed servers).
QUESTDB_INSECURE_SKIP_VERIFY=1 "$VENV/bin/pip" install -e . --force-reinstall --no-deps

echo "==> Verifying client"
"$VENV/bin/python" -c "import questdb; print('questdb', questdb.__version__, 'connect=', hasattr(questdb,'connect'))"

echo
echo "Done. Run the benchmark with:"
echo "  PYTHON=$VENV/bin/python ./bench.sh 50000000"
