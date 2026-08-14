from __future__ import annotations

import re
import stat
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


class DeploymentAssetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
        cls.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        cls.env = (DEPLOY / ".env.example").read_text(encoding="utf-8")
        cls.installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        cls.ctl = (DEPLOY / "axiomctl").read_text(encoding="utf-8")

    def test_compose_has_exact_production_services(self) -> None:
        service_block = self.compose.split("\nvolumes:", 1)[0]
        services = re.findall(r"^  ([a-z][a-z0-9-]*):$", service_block, re.MULTILINE)
        self.assertEqual(services, ["postgres", "migrator", "api", "runner"])

    def test_api_default_port_and_postgres_are_private(self) -> None:
        self.assertIn('"${AXIOM_PUBLISH_HOST:-127.0.0.1}:${AXIOM_PORT:-8888}:8899"', self.compose)
        self.assertIn("name: ${AXIOM_COMPOSE_PROJECT:-axiom}", self.compose)
        postgres_block = self.compose.split("  migrator:", 1)[0]
        self.assertNotIn("ports:", postgres_block)
        self.assertIn("AXIOM_DB_HOST: ${AXIOM_DB_HOST:-postgres}", self.compose)
        self.assertIn('AXIOM_DB_PORT: "${AXIOM_DB_PORT:-5432}"', self.compose)
        self.assertIn('host.docker.internal:host-gateway', self.compose)

    def test_runner_uses_compose_internal_api(self) -> None:
        self.assertIn("AXIOM_INTERNAL_SERVER_URL:-http://api:8899", self.compose)
        self.assertIn('"--api-url", "${AXIOM_INTERNAL_SERVER_URL:-http://api:8899}"', self.compose)
        self.assertNotIn("AXIOM_DATABASE_URL:", self.compose)
        self.assertNotIn("AXIOM_SERVER: http://127.0.0.1", self.compose)

    def test_runtime_is_required_and_node_is_on_path(self) -> None:
        self.assertIn("COPY runtime/ /opt/axiom/runtime/", self.dockerfile)
        self.assertIn("test -f /opt/axiom/runtime/manifest.json", self.dockerfile)
        self.assertIn("/opt/axiom/runtime/node/v22.22.2/bin", self.dockerfile)
        self.assertIn("AXIOM_RUNTIME_ROOT=/opt/axiom/runtime", self.dockerfile)
        self.assertIn("deploy/smoke_runtime.py", self.dockerfile)
        self.assertIn("RUN python /app/deploy/smoke_runtime.py", self.dockerfile)
        self.assertIn("/opt/axiom/release/manifest.json", self.dockerfile)
        self.assertIn("/opt/axiom/release/SHA256SUMS", self.dockerfile)
        self.assertIn("runtime-validator", self.dockerfile)

    def test_go_tool_builder_supports_katana_cgo_dependency(self) -> None:
        self.assertIn("ENV CGO_ENABLED=1", self.dockerfile)
        self.assertIn("ARG AXIOM_GOPROXY=https://proxy.golang.org,direct", self.dockerfile)
        self.assertIn("GOPROXY=${AXIOM_GOPROXY}", self.dockerfile)
        self.assertIn("build-essential pkg-config", self.dockerfile)
        self.assertNotIn("ENV CGO_ENABLED=0", self.dockerfile)

    def test_dockerignore_keeps_the_injected_pi_package(self) -> None:
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        node_modules = ignore.index("**/node_modules")
        self.assertGreater(ignore.index("!runtime/"), node_modules)
        self.assertGreater(ignore.index("!runtime/**"), node_modules)

    def test_browser_skill_uses_native_broker_without_legacy_scripts(self) -> None:
        skill = (ROOT / "src/axiom/dispatcher/skills/browser-control/SKILL.md").read_text(encoding="utf-8")
        scripts_dir = ROOT / "src/axiom/dispatcher/skills/browser-control/scripts"
        self.assertIn("内置 Extension", skill)
        self.assertIn("Browser Broker", skill)
        self.assertFalse(scripts_dir.exists())

    def test_env_has_pinned_image_and_internal_runtime_settings(self) -> None:
        version = (DEPLOY / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertIn(f"AXIOM_VERSION={version}", self.env)
        self.assertIn(f"AXIOM_IMAGE=ghcr.io/notstarr/axiom:{version}", self.env)
        self.assertIn("AXIOM_COMPOSE_PROJECT=axiom", self.env)
        self.assertIn("AXIOM_POSTGRES_IMAGE=postgres:18.4-bookworm@sha256:", self.env)
        self.assertIn("AXIOM_DB_HOST=postgres", self.env)
        self.assertIn("AXIOM_INTERNAL_SERVER_URL=http://api:8899", self.env)
        self.assertNotIn("latest", self.env)

    def test_installer_locks_default_asset_download_to_release_tag(self) -> None:
        self.assertIn('AXIOM_RELEASE_REF="${AXIOM_RELEASE_REF:-main}"', self.installer)
        self.assertIn('AXIOM_RELEASE_REF="v$AXIOM_VERSION"', self.installer)
        self.assertIn("AXIOM_RELEASE_BASE_URL", self.installer)
        self.assertIn("AXIOM_MIN_FREE_KB", self.installer)
        self.assertIn("port_in_use", self.installer)
        self.assertIn("read_env_value", self.installer)
        self.assertIn("persisted endpoint and image", self.installer)
        self.assertIn("AXIOM_POSTGRES_IMAGE", self.installer)

    def test_control_script_is_symlink_safe_and_preserves_data(self) -> None:
        self.assertIn("readlink -f", self.ctl)
        self.assertIn("--no-deps --force-recreate api runner", self.ctl)
        self.assertIn("previous .env restored", self.ctl)
        self.assertIn("pg_dump", self.ctl)
        self.assertIn('cp "$ENV_FILE" "$out_dir/.env"', self.ctl)
        self.assertIn("--volumes", self.ctl)
        self.assertIn("--rmi all", self.ctl)
        self.assertIn("pin_image_digest", self.ctl)
        self.assertIn("ROLLBACK_COMPLETED 1 \"$STATE_FILE\"", self.ctl)
        self.assertIn("database.rollback_compatible must be boolean", self.ctl)
        self.assertIn("candidate image validation failed", self.ctl)
        self.assertIn("fix forward", self.ctl)
        self.assertIn("/opt/axiom/release/manifest.json", self.ctl)

    def test_release_workflow_passes_tool_and_database_metadata(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("grep -E '^AXIOM_[A-Z0-9_]+=' deploy/tools.env >> \"$GITHUB_ENV\"", workflow)
        self.assertIn("AXIOM_DATABASE_MIGRATION_TARGET=${{ env.AXIOM_DATABASE_MIGRATION_TARGET }}", workflow)
        self.assertIn("AXIOM_DATABASE_ROLLBACK_COMPATIBLE=${{ env.AXIOM_DATABASE_ROLLBACK_COMPATIBLE }}", workflow)
        self.assertIn("platforms: linux/amd64", workflow)

    def test_deployment_scripts_are_executable(self) -> None:
        for path in (
            DEPLOY / "install.sh",
            DEPLOY / "smoke_runtime.py",
            DEPLOY / "axiomctl",
            DEPLOY / "verify_toolchain.sh",
            DEPLOY / "postgres-init/001-axiom-roles.sh",
            DEPLOY / "verify_runtime.py",
            DEPLOY / "generate_release_manifest.py",
        ):
            mode = path.stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, f"{path} must be executable")


if __name__ == "__main__":
    unittest.main()
