"""Durable-state reconciliation for AXIOM workers.

The dispatcher is deliberately independent from SQLAlchemy and FastAPI.  It
reads a snapshot through a control-plane port, calculates idempotent claim
actions, and applies those actions through the same port.  That separation is
what lets the scheduler be tested without a real Pi process and prevents a
worker from becoming a hidden second source of truth.
"""

from axiom.dispatcher.loop import Dispatcher

__all__ = ["Dispatcher"]
