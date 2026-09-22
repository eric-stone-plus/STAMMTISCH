#!/usr/bin/env bash
# Simulate the ci.yml python step's environment locally and report whether
# the suite would pass if the step were flipped to blocking right now.
#
# Modes (mirroring the two blocking-ization options in
# docs/ci-python-blocking.md):
#   bare          — exactly what ci.yml installs today (requirements-tui +
#                   numpy/pandas/pytest). The quantkit-gated tests are
#                   EXPECTED to fail here; the script reports their count.
#   --with-quantkit PATH
#                 — additionally installs quantkit from a local checkout
#                   (editable), simulating both Option A (published quantkit)
#                   and Option B (private checkout via PAT). Expected: zero
#                   failures. Only then is the flip to blocking safe.
#
# Exit codes: 0 = suite green in the requested mode (flip is safe);
#             1 = suite red (quantkit still missing or a real failure);
#             2 = environment setup failure.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODE="bare"
QK_PATH=""
for arg in "$@"; do
  case "$arg" in
    bare) MODE="bare" ;;
    --with-quantkit) MODE="quantkit" ;;
    *) QK_PATH="$arg" ;;
  esac
done
if [ "$MODE" = "quantkit" ] && [ ! -f "$QK_PATH/pyproject.toml" ]; then
  echo "usage: $0 bare | --with-quantkit /path/to/quantkit" >&2
  exit 2
fi

ENV="$(mktemp -d)"
trap 'rm -rf "$ENV"' EXIT
python3 -m venv "$ENV/venv" 2>/dev/null || { uv venv "$ENV/venv" -q; }
PIP="$ENV/venv/bin/pip"
PY="$ENV/venv/bin/python"
if [ ! -x "$PIP" ]; then PIP="$ENV/venv/bin/python -m pip"; fi
$PIP install -q --upgrade pip 2>/dev/null || true

echo "== mode: $MODE =="
echo "-- installing requirements-tui.txt + numpy pandas pytest (what ci.yml does today)"
$PIP install -q -r "$REPO/requirements-tui.txt" numpy pandas pytest

if [ "$MODE" = "quantkit" ]; then
  echo "-- additionally installing quantkit (editable, from $QK_PATH)"
  if command -v uv >/dev/null; then
    uv pip install -q --python "$ENV/venv/bin/python" -e "$QK_PATH" \
      || $PIP install -q -e "$QK_PATH"
  else
    $PIP install -q -e "$QK_PATH"
  fi
fi

cd "$REPO"
set +e
OUT="$("$PY" -m pytest services/tests/ interface/tests/ tui/tests/ -q -p no:cacheprovider 2>&1)"
RC=$?
set -e
echo "$OUT" | tail -3
FAILED=$(echo "$OUT" | grep -c '^FAILED' || true)
if [ "$RC" -eq 0 ]; then
  echo "RESULT: green — the ci.yml python step can be flipped to blocking in this mode."
  exit 0
fi
if [ "$MODE" = "bare" ]; then
  echo "RESULT: red as expected in bare mode — $FAILED quantkit-gated failures."
  echo "        (Flip is NOT safe until quantkit is installable: see docs/ci-python-blocking.md)"
  exit 1
fi
echo "RESULT: red in quantkit mode — UNEXPECTED. Investigate before flipping."
exit 1
