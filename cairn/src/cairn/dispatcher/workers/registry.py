from __future__ import annotations

from cairn.dispatcher.workers.adapters import PiDriver
from cairn.dispatcher.workers.base import WorkerDriver


DRIVERS: dict[str, WorkerDriver] = {
    "pi": PiDriver(),
}


def get_driver(name: str) -> WorkerDriver:
    return DRIVERS[name]
