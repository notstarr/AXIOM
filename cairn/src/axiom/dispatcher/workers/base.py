from __future__ import annotations

import abc
import shlex
from dataclasses import dataclass
from pathlib import Path

from axiom.dispatcher.config import WorkerConfig


@dataclass(slots=True)
class DriverResult:
    argv: list[str]
    session: str | None = None


class WorkerDriver(abc.ABC):
    type_name: str

    def supports_conclude(self) -> bool:
        return True

    def prepare_session(self) -> str | None:
        return None

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return self.build_healthcheck(worker)

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return shlex.join(self.build_startup_healthcheck(worker))

    @abc.abstractmethod
    def build_healthcheck(self, worker: WorkerConfig, phase: str = "") -> list[str]:
        raise NotImplementedError

    @abc.abstractmethod
    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None, phase: str = "") -> DriverResult:
        raise NotImplementedError

    @abc.abstractmethod
    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str, phase: str = "") -> list[str]:
        raise NotImplementedError

    def supports_rpc(self) -> bool:
        return False

    def build_rpc(self, worker: WorkerConfig, session: str | None, phase: str = "") -> list[str]:
        raise NotImplementedError

    def supports_compact_conclude(self) -> bool:
        """Whether the driver can conclude without replaying the execute session."""
        return False

    def build_compact_conclude(
        self,
        worker: WorkerConfig,
        prompt: str,
        phase: str = "",
    ) -> list[str]:
        raise NotImplementedError

    def session_size_bytes(self, worker: WorkerConfig, session_id: str | None) -> int | None:
        return None

    def export_compact_evidence(
        self,
        worker: WorkerConfig,
        session_id: str,
        destination: Path,
    ) -> Path | None:
        return None

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        return session

    def recover_session(
        self,
        worker: WorkerConfig,
        session: str | None,
        stdout: str,
        stderr: str,
    ) -> str | None:
        """Return the real resumable session created by an execute attempt."""
        return self.extract_session(session, stdout, stderr)

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return stdout
