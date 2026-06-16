#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"
OOPTDD=("$PYTHON" -m ooptdd_loop.cli)
TMPDIR="$(mktemp -d)"
INCLUDE_EXTERNAL=0

cleanup() {
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

usage() {
  cat <<'USAGE'
Usage: scripts/verify_ooptdd.sh [--include-external]

Runs the local OOPTDD verification harness:
  - ruff
  - focused pytest runtime/OTel tests
  - full pytest suite
  - CLI metadata/spec validation
  - memory-backed example specs
  - golden save/diff smoke
  - MCP metadata check
  - MCP stdio roundtrip smoke
  - Claude/Codex MCP config generation

Options:
  --include-external   also run logserver/OpenObserve checks when configured
  -h, --help           show this help
USAGE
}

while (($#)); do
  case "$1" in
    --include-external)
      INCLUDE_EXTERNAL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

step() {
  printf '\n==> %s\n' "$1"
}

run() {
  step "$1"
  shift
  "$@"
}

run_capture() {
  local label="$1"
  local log="$2"
  shift 2
  step "$label"
  if "$@" >"$log" 2>&1; then
    printf 'ok: %s\n' "$label"
  else
    cat "$log" >&2
    return 1
  fi
}

assert_json() {
  local file="$1"
  "$PYTHON" - "$file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    json.load(fh)
PY
}

run "ruff" ruff check .
run "pytest runtime/OTel focus" "$PYTHON" -m pytest -q tests/test_pytest_plugin.py tests/test_otel.py
run "pytest full suite" "$PYTHON" -m pytest -q

run_capture "CLI tools registry" "$TMPDIR/tools.json" "${OOPTDD[@]}" tools --json
assert_json "$TMPDIR/tools.json"

run_capture "CLI harness profile" "$TMPDIR/harness-profile.json" \
  "${OOPTDD[@]}" harness-profile --json
assert_json "$TMPDIR/harness-profile.json"

run_capture "methodology validation" "$TMPDIR/validate-ooptdd.json" \
  "${OOPTDD[@]}" validate-spec example/requirements_ooptdd.yaml --json
assert_json "$TMPDIR/validate-ooptdd.json"

run_capture "MCP metadata check" "$TMPDIR/mcp-check.json" "${OOPTDD[@]}" mcp --check
assert_json "$TMPDIR/mcp-check.json"

run_capture "MCP stdio roundtrip smoke" "$TMPDIR/mcp-stdio-smoke.json" \
  "$PYTHON" scripts/mcp_stdio_smoke.py --run
assert_json "$TMPDIR/mcp-stdio-smoke.json"

run_capture "Claude/Codex MCP config generation" "$TMPDIR/mcp-config.json" \
  "${OOPTDD[@]}" mcp-config --json
assert_json "$TMPDIR/mcp-config.json"

for spec in \
  example/requirements.yaml \
  example/requirements_ooptdd.yaml \
  example/requirements_selectors.yaml \
  example/requirements_local_capture.yaml \
  example/requirements_ontology.yaml
do
  safe_name="${spec//\//_}"
  run_capture "run $spec" "$TMPDIR/${safe_name}.json" "${OOPTDD[@]}" run "$spec" --json
  assert_json "$TMPDIR/${safe_name}.json"
done

GOLDEN="$TMPDIR/selectors-golden.json"
run_capture "golden save" "$TMPDIR/golden-save.json" \
  "${OOPTDD[@]}" golden save example/requirements_selectors.yaml \
    --run \
    --cid verify-golden \
    --out "$GOLDEN"
assert_json "$TMPDIR/golden-save.json"

run_capture "golden diff" "$TMPDIR/golden-diff.json" \
  "${OOPTDD[@]}" golden diff example/requirements_selectors.yaml "$GOLDEN" \
    --run \
    --cid verify-golden
assert_json "$TMPDIR/golden-diff.json"

if (( INCLUDE_EXTERNAL )); then
  run_capture "logserver health" "$TMPDIR/logserver-health.json" \
    "${OOPTDD[@]}" logserver-health
  assert_json "$TMPDIR/logserver-health.json"

  if [[ -n "${OOPTDD_OO_URL:-}" && -n "${OOPTDD_OO_PASSWORD:-}" ]]; then
    run_capture "OpenObserve logserver MCP pilot" "$TMPDIR/logserver-pilot.json" \
      "${OOPTDD[@]}" run example/requirements_logserver_mcp.yaml \
        --cid verify-logserver-mcp \
        --json
    assert_json "$TMPDIR/logserver-pilot.json"

    run_capture "pytest xdist/OTel OpenObserve pilot" "$TMPDIR/pytest-xdist-otel-pilot.log" \
      scripts/real_backend_xdist_otel_pilot.sh
  else
    printf 'skip: OpenObserve pilot needs OOPTDD_OO_URL and OOPTDD_OO_PASSWORD\n'
  fi
fi

printf '\nOOPTDD verification passed.\n'
