from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path



SCRIPT = (
    Path(__file__).parents[1]
    / "src"
    / "axiom"
    / "dispatcher"
    / "skills"
    / "ffuf"
    / "scripts"
    / "ffuf_background.sh"
)


def _fake_ffuf(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "ffuf"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import signal
import sys
import time
from pathlib import Path

args = sys.argv[1:]
args_file = os.environ.get("FAKE_FFUF_ARGS_FILE")
if args_file:
    Path(args_file).write_text(json.dumps(args), encoding="utf-8")


def output_path(arguments):
    for index, argument in enumerate(arguments[:-1]):
        if argument in ("-o", "--output"):
            return arguments[index + 1]
        if argument.startswith("--output="):
            return argument.split("=", 1)[1]
    return None


def stop(_signum, _frame):
    raise SystemExit(143)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
if os.environ.get("FAKE_FFUF_SLEEP"):
    while True:
        print("fake ffuf running", flush=True)
        time.sleep(0.1)

result = output_path(args)
if result:
    Path(result).write_text(
        json.dumps({{"results": [{{"input": "admin", "status": 200}}]}}),
        encoding="utf-8",
    )
print("fake ffuf complete", flush=True)
sys.exit(int(os.environ.get("FAKE_FFUF_EXIT", "0")))
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _environment(tmp_path: Path, fake_ffuf: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["AXIOM_RUN_DIR"] = str(tmp_path / "run")
    environment["AXIOM_PYTHON"] = sys.executable
    environment["PATH"] = f"{fake_ffuf.parent}{os.pathsep}{environment['PATH']}"
    environment["FAKE_FFUF_ARGS_FILE"] = str(tmp_path / "ffuf-args.json")
    return environment


def _run(
    environment: dict[str, str], *arguments: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=check,
    )


def _fields(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line and not line.startswith("{"):
            key, value = line.split("=", 1)
            if key and " " not in key:
                fields[key] = value
    return fields


def _wait_for_status(environment: dict[str, str], run_id: str, expected: str) -> str:
    deadline = time.monotonic() + 5
    last = ""
    while time.monotonic() < deadline:
        result = _run(environment, "status", run_id)
        assert result.returncode == 0, result.stderr
        last = _fields(result.stdout).get("status", "")
        if last == expected:
            return result.stdout
        time.sleep(0.05)
    raise AssertionError(f"expected {expected}, got {last}: {result.stdout}")


def test_start_preserves_native_args_and_manages_output(tmp_path: Path) -> None:
    fake = _fake_ffuf(tmp_path)
    environment = _environment(tmp_path, fake)
    native = [
        "-w",
        "evidence/dict/params.txt:PARAM",
        "-u",
        "https://authorized.example/api?PARAM=value&fixed=1",
        "-H",
        "X-Test: VALUE",
        "-H",
        "Cookie: session=test",
        "-d",
        '{"id":"VALUE"}',
        "-rate",
        "7",
        "-timeout",
        "4",
    ]

    started = _run(environment, "start", "--name", "api params", "--", *native)
    assert started.returncode == 0, started.stderr
    fields = _fields(started.stdout)
    run_id = fields["run_id"]
    assert run_id.startswith("ffuf-")
    assert fields["status"] == "running"
    assert fields["pid"].isdigit()
    assert fields["result_file"].endswith("/results.json")
    assert fields["log_file"].endswith("/run.log")

    completed = _wait_for_status(environment, run_id, "completed")
    assert "exit_code=0" in completed

    forwarded = json.loads((tmp_path / "ffuf-args.json").read_text(encoding="utf-8"))
    assert forwarded[: len(native)] == native
    assert forwarded[len(native) :] == [
        "-noninteractive",
        "-of",
        "json",
        "-o",
        fields["result_file"],
    ]

    result_payload = json.loads(Path(fields["result_file"]).read_text(encoding="utf-8"))
    assert result_payload["results"][0]["input"] == "admin"
    output = _run(environment, "output", run_id, "--lines", "20")
    assert output.returncode == 0, output.stderr
    assert '"results"' in output.stdout
    assert "fake ffuf complete" in output.stdout

    listing = _run(environment, "list")
    assert listing.returncode == 0, listing.stderr
    assert f"{run_id}\tcompleted" in listing.stdout


def test_stop_terminates_long_running_ffuf_and_reports_stopped(tmp_path: Path) -> None:
    fake = _fake_ffuf(tmp_path)
    environment = _environment(tmp_path, fake)
    environment["FAKE_FFUF_SLEEP"] = "1"

    started = _run(environment, "start", "--", "-w", "words.txt", "-u", "https://example.test/FUZZ")
    assert started.returncode == 0, started.stderr
    fields = _fields(started.stdout)
    run_id = fields["run_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not (tmp_path / "ffuf-args.json").exists():
        time.sleep(0.05)
    assert (tmp_path / "ffuf-args.json").exists()

    stopped = _run(environment, "stop", run_id)
    assert stopped.returncode == 0, stopped.stderr
    assert _fields(stopped.stdout)["status"] == "stopped"
    assert (Path(fields["result_file"]).parent / "stop.requested").exists()
    assert (Path(fields["result_file"]).parent / "exit.code").read_text(encoding="utf-8").strip() == "143"


def test_rejects_script_managed_output_flags(tmp_path: Path) -> None:
    fake = _fake_ffuf(tmp_path)
    environment = _environment(tmp_path, fake)
    for output_flag in ("-o", "--output", "-of", "--output-format", "-od", "--output-dir", "--debug-log", "--output=custom.json"):
        result = _run(
            environment,
            "start",
            "--",
            "-w",
            "words.txt",
            "-u",
            "https://example.test/FUZZ",
            output_flag,
            "custom.json" if "=" not in output_flag else "",
        )
        assert result.returncode == 2, (output_flag, result.stdout, result.stderr)
        assert "managed by this script" in result.stderr


def test_rejects_path_traversal_and_symlinked_run_directory(tmp_path: Path) -> None:
    fake = _fake_ffuf(tmp_path)
    environment = _environment(tmp_path, fake)
    run_root = Path(environment["AXIOM_RUN_DIR"]) / "evidence" / "ffuf"
    run_root.mkdir(parents=True)

    traversal = _run(environment, "status", "ffuf-../../outside")
    assert traversal.returncode == 2
    assert "invalid run_id" in traversal.stderr

    outside = tmp_path / "outside"
    outside.mkdir()
    (run_root / "ffuf-link").symlink_to(outside, target_is_directory=True)
    linked = _run(environment, "status", "ffuf-link")
    assert linked.returncode == 2
    assert "run not found" in linked.stderr
