from __future__ import annotations

import threading
from collections.abc import Callable


class LeaseLostError(RuntimeError):
    pass


class LeaseHeartbeat:
    """Send an immediate heartbeat, then keep the lease alive in the background."""

    def __init__(
        self,
        send: Callable[[], None],
        *,
        interval_seconds: float = 10.0,
        max_consecutive_failures: int = 3,
    ):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_consecutive_failures <= 0:
            raise ValueError("max_consecutive_failures must be positive")
        self._send = send
        self._interval = interval_seconds
        self._max_failures = max_consecutive_failures
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat already started")
        self._beat()
        self._thread = threading.Thread(
            target=self._run,
            name="axiom-lease-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._interval * 2, 1.0))

    def ensure_active(self) -> None:
        if self._lost.is_set():
            raise LeaseLostError("Intent lease heartbeat was lost") from self._last_error

    def is_active(self) -> bool:
        return not self._lost.is_set() and not self._stop.is_set()

    def _run(self) -> None:
        failures = 0
        while not self._stop.wait(self._interval):
            try:
                self._send()
                failures = 0
            except BaseException as exc:
                failures += 1
                self._last_error = exc
                if failures >= self._max_failures:
                    self._lost.set()
                    return

    def _beat(self) -> None:
        try:
            self._send()
        except BaseException as exc:
            self._last_error = exc
            self._lost.set()
            raise LeaseLostError("Initial Intent heartbeat failed") from exc
