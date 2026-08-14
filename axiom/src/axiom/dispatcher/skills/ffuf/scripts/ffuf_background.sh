#!/usr/bin/env bash
# Run FFUF as a small, task-local background job while preserving native FFUF flags.
# The model only manages the FFUF arguments; this script owns paths, logging, PID,
# JSON output, status, result collection, and stopping the job.
set -euo pipefail

PYTHON_BIN="${AXIOM_PYTHON:-python3}"
RUN_ROOT="${AXIOM_RUN_DIR:-$PWD}"
RUNS_ROOT="$RUN_ROOT/evidence/ffuf"

usage() {
  cat >&2 <<'USAGE'
Usage:
  ffuf_background.sh start [--name LABEL] -- <native ffuf arguments...>
  ffuf_background.sh status <run_id>
  ffuf_background.sh output <run_id> [--lines N]
  ffuf_background.sh stop <run_id>
  ffuf_background.sh list

All arguments after `--` are passed to ffuf unchanged. The script manages
nohup, background execution, task-local logs, PID, JSON output and exit status.
USAGE
}

die() {
  echo "ffuf_background: $*" >&2
  exit 2
}

require_run_id() {
  local run_id="${1:-}"
  [[ "$run_id" =~ ^ffuf-[A-Za-z0-9T._-]+$ ]] || die "invalid run_id"
  local run_dir="$RUNS_ROOT/$run_id"
  [[ -d "$run_dir" && ! -L "$run_dir" ]] || die "run not found: $run_id"
  local root_real run_real
  root_real="$(cd -- "$RUNS_ROOT" 2>/dev/null && pwd -P)" || die "invalid run root"
  run_real="$(cd -- "$run_dir" 2>/dev/null && pwd -P)" || die "run not found: $run_id"
  [[ "$run_real" == "$root_real"/* ]] || die "invalid run path"
  printf '%s\n' "$run_real"
}

read_first_line() {
  local path="$1"
  local line=""
  if [[ -f "$path" ]]; then
    IFS= read -r line < "$path" || true
    printf '%s\n' "$line"
  fi
}

is_running() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # macOS/Linux can leave a just-exited child as a zombie briefly. Treat it
  # as stopped so status/stop do not report a completed runner as running.
  local state
  state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
  state="${state//[[:space:]]/}"
  [[ -n "$state" && "$state" != Z* ]]
}

status_value() {
  local run_dir="$1"
  local pid
  pid="$(read_first_line "$run_dir/pid")"
  if is_running "$pid"; then
    printf '%s\n' "running"
    return
  fi
  if [[ -f "$run_dir/stop.requested" ]]; then
    printf '%s\n' "stopped"
  elif [[ -f "$run_dir/exit.code" ]]; then
    local exit_code
    exit_code="$(read_first_line "$run_dir/exit.code")"
    if [[ "$exit_code" == "0" ]]; then
      printf '%s\n' "completed"
    else
      printf '%s\n' "failed"
    fi
  else
    printf '%s\n' "unknown"
  fi
}

print_status() {
  local run_id="$1"
  local run_dir
  run_dir="$(require_run_id "$run_id")"
  local pid status exit_code started finished result_file log_file
  pid="$(read_first_line "$run_dir/pid")"
  status="$(status_value "$run_dir")"
  exit_code="$(read_first_line "$run_dir/exit.code")"
  started="$(read_first_line "$run_dir/started_at")"
  finished="$(read_first_line "$run_dir/finished_at")"
  result_file="$run_dir/results.json"
  log_file="$run_dir/run.log"

  printf 'run_id=%s\n' "$run_id"
  printf 'status=%s\n' "$status"
  printf 'pid=%s\n' "${pid:-unknown}"
  printf 'started_at=%s\n' "${started:-unknown}"
  [[ -n "$finished" ]] && printf 'finished_at=%s\n' "$finished"
  [[ -n "$exit_code" ]] && printf 'exit_code=%s\n' "$exit_code"
  printf 'result_file=%s\n' "$result_file"
  printf 'log_file=%s\n' "$log_file"
}

reject_managed_output_flags() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      -o|--output|-of|--output-format|-od|--output-dir|-debug-log|--debug-log|-o=*|--output=*|-of=*|--output-format=*|-od=*|--output-dir=*|-debug-log=*|--debug-log=*)
        die "$arg is managed by this script; omit it and use the returned result_file"
        ;;
    esac
  done
}

start_run() {
  shift
  local label=""
  if [[ "${1:-}" == "--name" || "${1:-}" == "--label" ]]; then
    [[ $# -ge 2 ]] || die "$1 requires a value"
    label="$2"
    shift 2
  fi
  if [[ "${1:-}" == "--" ]]; then
    shift
  fi
  (($# > 0)) || die "start requires native ffuf arguments"
  command -v ffuf >/dev/null 2>&1 || die "ffuf not found on PATH"
  reject_managed_output_flags "$@"

  local suffix
  suffix="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  if [[ -n "$label" ]]; then
    label="${label//[^A-Za-z0-9._-]/-}"
    suffix="${suffix}-${label}"
  fi
  local run_id="ffuf-$suffix"
  local run_dir="$RUNS_ROOT/$run_id"
  mkdir -p "$run_dir"
  chmod 700 "$run_dir"

  local runner="$run_dir/.runner.sh"
  local result_file="$run_dir/results.json"
  local log_file="$run_dir/run.log"
  local started_at
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s\n' "$started_at" > "$run_dir/started_at"
  printf '%s\n' "$label" > "$run_dir/label"
  rm -f "$run_dir/stop.requested" "$run_dir/exit.code" "$run_dir/finished_at" "$result_file"

  # Keep native ffuf arguments out of a generated command string. They are
  # passed as positional arguments, so URLs, JSON bodies and repeated flags keep
  # their original shell argument boundaries.
  cat > "$runner" <<'RUNNER'
#!/usr/bin/env bash
set -u

child_pid=""
finish() {
  local code="$1"
  printf '%s\n' "$code" > "$FFUF_EXIT_CODE_PATH"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$FFUF_FINISHED_AT_PATH"
  exit "$code"
}

stop_child() {
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$child_pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$child_pid" 2>/dev/null || true
  fi
  finish 143
}

trap stop_child TERM INT
ffuf "$@" -noninteractive -of json -o "$FFUF_RESULT_PATH" &
child_pid="$!"
wait "$child_pid"
exit_code="$?"
finish "$exit_code"
RUNNER
  chmod 700 "$runner"

  export FFUF_RESULT_PATH="$result_file"
  export FFUF_EXIT_CODE_PATH="$run_dir/exit.code"
  export FFUF_FINISHED_AT_PATH="$run_dir/finished_at"

  # Deliberately use nohup + & here. The script owns the background process
  # details so the model does not need to compose them on every invocation.
  nohup bash "$runner" "$@" > "$log_file" 2>&1 < /dev/null &
  local pid="$!"
  printf '%s\n' "$pid" > "$run_dir/pid"

  printf 'run_id=%s\n' "$run_id"
  printf 'status=running\n'
  printf 'pid=%s\n' "$pid"
  printf 'result_file=%s\n' "$result_file"
  printf 'log_file=%s\n' "$log_file"
}

output_run() {
  shift
  local run_id="${1:-}"
  [[ -n "$run_id" ]] || die "output requires run_id"
  shift || true
  local lines=80
  if [[ "${1:-}" == "--lines" ]]; then
    [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "--lines requires a positive integer"
    lines="$2"
  fi
  local run_dir
  run_dir="$(require_run_id "$run_id")"
  printf 'run_id=%s\n' "$run_id"
  printf 'status=%s\n' "$(status_value "$run_dir")"
  if [[ -s "$run_dir/results.json" ]]; then
    printf '%s\n' 'results_json='
    # Keep tool output bounded; the complete file remains available to read.
    "$PYTHON_BIN" - "$run_dir/results.json" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
limit = 1024 * 1024
payload = path.read_bytes()
if len(payload) <= limit:
    sys.stdout.buffer.write(payload)
else:
    sys.stdout.buffer.write(payload[:limit])
    sys.stdout.write("\n[results truncated; read the complete results.json from the path below]\n")
PY
    printf '\n'
  else
    printf '%s\n' 'results_json=not_ready'
  fi
  printf 'log_tail=\n'
  tail -n "$lines" "$run_dir/run.log" 2>/dev/null || true
  printf 'result_file=%s\n' "$run_dir/results.json"
  printf 'log_file=%s\n' "$run_dir/run.log"
}

stop_run() {
  shift
  local run_id="${1:-}"
  [[ -n "$run_id" ]] || die "stop requires run_id"
  local run_dir
  run_dir="$(require_run_id "$run_id")"
  local pid
  pid="$(read_first_line "$run_dir/pid")"
  : > "$run_dir/stop.requested"
  if is_running "$pid"; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      is_running "$pid" || break
      sleep 1
    done
    if is_running "$pid"; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  print_status "$run_id"
}

list_runs() {
  mkdir -p "$RUNS_ROOT"
  local run_dir run_id
  shopt -s nullglob
  for run_dir in "$RUNS_ROOT"/ffuf-*; do
    [[ -d "$run_dir" ]] || continue
    run_id="${run_dir##*/}"
    printf '%s\t%s\tpid=%s\n' "$run_id" "$(status_value "$run_dir")" "$(read_first_line "$run_dir/pid")"
  done
}

case "${1:-}" in
  start) start_run "$@" ;;
  status) [[ $# -eq 2 ]] || die "status requires exactly one run_id"; print_status "$2" ;;
  output) output_run "$@" ;;
  stop) [[ $# -eq 2 ]] || die "stop requires exactly one run_id"; stop_run "$@" ;;
  list) [[ $# -eq 1 ]] || die "list does not accept arguments"; list_runs ;;
  -h|--help|"") usage; exit 0 ;;
  *) usage; exit 2 ;;
esac
