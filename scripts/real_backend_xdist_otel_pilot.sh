#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"
CID="${OOPTDD_PILOT_CID:-}"
TRACE_PARENT="${TRACEPARENT:-}"
PASSES="${OOPTDD_PILOT_PASSES:-5}"
PASS_DELAY="${OOPTDD_PILOT_PASS_DELAY:-2}"
MINUTES_BACK="${OOPTDD_PILOT_MINUTES_BACK:-30}"
TMPDIR="$(mktemp -d)"
REPORT="$TMPDIR/pytest-logserver-xdist-report.json"
TRACE_LOG="$TMPDIR/logserver-trace.json"

cleanup() {
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

usage() {
  cat <<'USAGE'
Usage: scripts/real_backend_xdist_otel_pilot.sh

Runs the real-backend OOPTDD pilot:
  pytest + xdist + --ooptdd-spec + TRACEPARENT + OpenObserve readback +
  logserver MCP trace lookup.

Required env:
  OOPTDD_OO_URL
  OOPTDD_OO_PASSWORD

Optional env:
  OOPTDD_OO_USER              OpenObserve user, default root
  OOPTDD_OO_ORG               OpenObserve org, default default
  OO_MCP_URL                  upstream logserver MCP URL
  TRACEPARENT                 W3C traceparent; generated if unset
  OOPTDD_PILOT_CID            run cid; generated if unset
  OOPTDD_PILOT_PASSES         OOPTDD evaluation attempts, default 5
  OOPTDD_PILOT_PASS_DELAY     seconds between attempts, default 2
  OOPTDD_PILOT_MINUTES_BACK   logserver trace window, default 30
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_env() {
  local key="$1"
  if [[ -z "${!key:-}" ]]; then
    echo "$key is required for the real backend pilot" >&2
    usage >&2
    exit 2
  fi
}

json_check() {
  "$PYTHON" - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    json.load(fh)
PY
}

require_env OOPTDD_OO_URL
require_env OOPTDD_OO_PASSWORD

if [[ -z "$CID" ]]; then
  CID="$("$PYTHON" - <<'PY'
import uuid
print(f"pytest-xdist-real-{uuid.uuid4().hex[:12]}")
PY
)"
fi

if [[ -z "$TRACE_PARENT" ]]; then
  TRACE_PARENT="$("$PYTHON" - <<'PY'
import secrets
print(f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01")
PY
)"
fi

echo "cid=$CID"
echo "traceparent=$TRACE_PARENT"

echo
echo "==> logserver health"
"$PYTHON" -m ooptdd_loop.cli logserver-health >"$TMPDIR/logserver-health.json"
json_check "$TMPDIR/logserver-health.json"

echo
echo "==> pytest xdist OpenObserve pilot"
"$PYTHON" -m pytest -q \
  -p no:ooptdd_loop \
  -p xdist \
  -p ooptdd_loop.pytest_plugin \
  -n 2 \
  example/test_pytest_logserver_xdist.py \
  --ooptdd-spec example/requirements_pytest_logserver_xdist.yaml \
  --ooptdd-cid "$CID" \
  --ooptdd-trace-parent "$TRACE_PARENT" \
  --ooptdd-passes "$PASSES" \
  --ooptdd-pass-delay "$PASS_DELAY" \
  --ooptdd-report "$REPORT"

json_check "$REPORT"
"$PYTHON" - "$REPORT" "$TRACE_PARENT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    payload = json.load(fh)

traceparent = sys.argv[2]
assert payload["complete"] is True, payload
assert payload["trace_parent"] == traceparent, payload
assert payload["xdist"]["forwarded_events"] == 0, payload
assert payload["done"] == payload["total"] == 2, payload
assert payload["evaluation_attempts"] >= 1, payload
print(
    "ooptdd report ok:",
    f"done={payload['done']}/{payload['total']}",
    f"attempts={payload['evaluation_attempts']}",
)
PY

echo
echo "==> logserver trace"
"$PYTHON" -m ooptdd_loop.cli logserver-trace "$CID" --minutes-back "$MINUTES_BACK" >"$TRACE_LOG"
json_check "$TRACE_LOG"
"$PYTHON" - "$TRACE_LOG" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    payload = json.load(fh)

assert payload.get("reachable"), payload
print("logserver trace ok")
PY

echo
echo "Real backend xdist/OTel pilot passed."
