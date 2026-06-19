"""crawler package — split from the original monolithic crawler.py.

Public API:
    router              — FastAPI APIRouter (imported by app.py)
    run_crawler_supervisor — supervisor entry point (imported by cli.py)
    state_view_exists   — check if a crawler state view has data (imported by files.py)
    render_state_snapshot — render a YAML view from DB state (imported by files.py)
"""

from .router import router
from .supervisor import run_crawler_supervisor
from .state import state_view_exists, render_state_snapshot

# Backward-compat aliases for files.py which imports these as module-level
# attributes: crawler_router._crawler_state_view_exists / _render_crawler_state_snapshot
_crawler_state_view_exists = state_view_exists
_render_crawler_state_snapshot = render_state_snapshot

__all__ = [
    "router",
    "run_crawler_supervisor",
    "state_view_exists",
    "render_state_snapshot",
]
