from __future__ import annotations

from axiom.dispatcher.workers.adapters.pi import PiDriver
from axiom.dispatcher.workers.base import WorkerDriver


DRIVERS: dict[str, WorkerDriver] = {
    "pi": PiDriver(),
}


def get_driver(name: str) -> WorkerDriver:
    return DRIVERS[name]
