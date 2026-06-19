from __future__ import annotations

import abc
import shlex
from dataclasses import dataclass

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

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        return session

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return stdout
