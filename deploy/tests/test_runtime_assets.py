from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "deploy" / "verify_runtime.py"
SMOKE = ROOT / "deploy" / "smoke_runtime.py"
GENERATOR = ROOT / "deploy" / "generate_release_manifest.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RuntimeAssetTests(unittest.TestCase):
    def make_runtime(self, root: Path, *, manifest: bool = True) -> tuple[Path, Path]:
        pi_dir = root / "pi" / "0.82.1"
        node = root / "node" / "v22.22.2" / "bin" / "node"
        pi = pi_dir / "bin" / "pi"
        package = pi_dir / "node_modules" / "@earendil-works" / "pi-coding-agent"
        entry = package / "dist" / "cli.js"
        node.parent.mkdir(parents=True)
        pi.parent.mkdir(parents=True)
        entry.parent.mkdir(parents=True)
        node.write_text("#!/bin/sh\nprintf 'v22.22.2\\n'\n", encoding="utf-8")
        pi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for path in (node, pi, entry):
            path.chmod(0o755)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "@earendil-works/pi-coding-agent",
                    "version": "0.82.1",
                    "bin": {"pi": "dist/cli.js"},
                }
            ),
            encoding="utf-8",
        )
        if manifest:
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "os": "linux",
                        "architecture": "x86_64",
                        "pi_version": "0.82.1",
                        "node_version": "v22.22.2",
                        "pi_package": "@earendil-works/pi-coding-agent",
                        "pi_entry": "dist/cli.js",
                        "pi_sha256": digest(pi),
                        "node_sha256": digest(node),
                        "source": "test-fixture",
                        "built_at": "2026-08-09T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
        return pi, node

    def run_verify(self, root: Path) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "AXIOM_RUNTIME_ROOT": str(root)}
        return subprocess.run(
            [sys.executable, str(VERIFY)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_manifest_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_runtime(root, manifest=False)
            result = self.run_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("runtime manifest is missing", result.stderr)

    def test_manifest_schema_and_hashes_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_runtime(root)
            result = self.run_verify(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"status": "ok"', result.stdout)

            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            manifest["pi_sha256"] = "0" * 64
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Pi SHA-256 does not match", result.stderr)

    def test_manifest_rejects_wrong_platform_and_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_runtime(root)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            manifest["architecture"] = "arm64"
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("architecture", result.stderr)

    def test_pi_json_rpc_and_extensions_smoke_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pi, _ = self.make_runtime(root, manifest=False)
            pi.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'--mode json'*) printf '%s\\n' '{\"type\":\"session\"}' '{\"type\":\"agent_start\"}'; sleep 5;;\n"
                "  *'--mode rpc'*) sleep 5;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                encoding="utf-8",
            )
            pi.chmod(0o755)
            extension_root = root / "extensions"
            for name in ("axiom-guard", "axiom-browser", "axiom-mcp-tools", "axiom-tool-activation"):
                path = extension_root / name / "index.ts"
                path.parent.mkdir(parents=True)
                path.write_text("export default function () {}\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SMOKE),
                    "--runtime-root",
                    str(root),
                    "--extension-root",
                    str(extension_root),
                    "--startup-timeout",
                    "3",
                ],
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"status": "ok"', result.stdout)


    def make_fake_toolchain(self, bin_dir: Path, *, failing: str | None = None) -> None:
        versions = {
            "python": "Python 3.12.11",
            "curl": "curl 8.14.1",
            "jq": "jq-1.7.1",
            "nmap": "Nmap version 7.95",
            "ffuf": "Fuzz Faster U Fool - v2.1.0",
            "katana": "katana v1.1.3",
            "httpx": "httpx v1.6.10",
            "subfinder": "subfinder v2.6.7",
            "nuclei": "nuclei v3.3.7",
            "sqlmap": "sqlmap version 1.9.8",
            "chromium": "Chromium 138.0.7204.157",
        }
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name, output in versions.items():
            path = bin_dir / name
            if name == failing:
                script = "#!/bin/sh\necho tool failure >&2\nexit 7\n"
            else:
                script = f"#!/bin/sh\nprintf '%s\\n' {output!r}\n"
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)

    def run_manifest_generator(
        self,
        runtime_manifest: Path,
        output: Path,
        tool_bin: Path,
        *,
        rollback: str = "0",
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{tool_bin}:{os.environ.get('PATH', '')}",
            "CHROME_BIN": str(tool_bin / "chromium"),
            "AXIOM_BUILD_VERSION": "0.1.0",
            "AXIOM_BUILD_REVISION": "test-revision",
            "AXIOM_BUILD_DATE": "2026-08-09T00:00:00Z",
            "AXIOM_DATABASE_MIGRATION_TARGET": "postgresql@head",
            "AXIOM_DATABASE_ROLLBACK_COMPATIBLE": rollback,
        }
        return subprocess.run(
            [sys.executable, str(GENERATOR), "--runtime-manifest", str(runtime_manifest), "--output", str(output)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_release_manifest_contains_database_and_tool_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool_bin = root / "bin"
            self.make_fake_toolchain(tool_bin)
            runtime_manifest = root / "runtime.json"
            runtime_manifest.write_text(json.dumps({"pi_version": "0.82.1"}), encoding="utf-8")
            output = root / "release" / "manifest.json"
            result = self.run_manifest_generator(runtime_manifest, output, tool_bin, rollback="false")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["database"], {"migration_target": "postgresql@head", "rollback_compatible": False})
            self.assertEqual(payload["tools"]["ffuf"], "Fuzz Faster U Fool - v2.1.0")
            self.assertEqual(set(payload["tools"]), {
                "python", "curl", "jq", "nmap", "ffuf", "katana", "httpx",
                "subfinder", "nuclei", "sqlmap", "chromium",
            })

    def test_release_manifest_fails_when_tool_version_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool_bin = root / "bin"
            self.make_fake_toolchain(tool_bin, failing="nuclei")
            runtime_manifest = root / "runtime.json"
            runtime_manifest.write_text("{}", encoding="utf-8")
            result = self.run_manifest_generator(runtime_manifest, root / "manifest.json", tool_bin)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("nuclei", result.stderr)

    def test_release_manifest_rejects_invalid_rollback_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool_bin = root / "bin"
            self.make_fake_toolchain(tool_bin)
            runtime_manifest = root / "runtime.json"
            runtime_manifest.write_text("{}", encoding="utf-8")
            result = self.run_manifest_generator(runtime_manifest, root / "manifest.json", tool_bin, rollback="maybe")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("AXIOM_DATABASE_ROLLBACK_COMPATIBLE", result.stderr)

if __name__ == "__main__":
    unittest.main()
