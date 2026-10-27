"""Assemble the attack path that leads *into* one finding or Result.

A worker executing one Intent sees only its own slice of a long engagement: it holds a
panel token and can reach an internal host, but it has no idea the token came from a
cloud-metadata SSRF four Intents ago.  Asking it to write that history is asking it to
invent it — so the history is read from the graph instead.

The graph already carries it:

* ``intents.to_fact_id`` — the fact an Intent produced
* ``intent_sources (intent_id, fact_id)`` — the facts an Intent consumed
* ``red_team_results (chain_id, intent_id)`` — which Result was produced where

``static/js/lineage.js`` derives cross-chain edges from exactly these tables for the
lineage view.  This module is the same walk done in SQL, for the report.

Only red-team tasks get a path.  A vulnerability-mode kill chain normally closes inside
one Intent and the reviewer replays it, so a separate chain adds nothing.

Points are deliberately absent: this is the "how did we get here" narrative that a
human reads, not the scoring record.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, select

from axiom.modules.graph.tables import intents, intent_sources
from axiom.modules.redteam.tables import red_team_results


MAX_PATH_LINKS = 20
_GAIN_FALLBACK_CHARS = 80
_ELLIPSIS = "…（更早 {count} 环已省略）"


@dataclass(frozen=True, slots=True)
class PathLink:
    """One already-played step on the way to the reported Result."""

    intent_id: str
    chain_id: str
    title: str
    gain: str
    is_current: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "chain_id": self.chain_id,
            "title": self.title,
            "gain": self.gain,
            "is_current": self.is_current,
        }


def attack_path(
    conn: Connection,
    *,
    task_id: str,
    intent_id: str,
    chain_id: str | None = None,
    max_links: int = MAX_PATH_LINKS,
) -> list[PathLink]:
    """Return the ordered links that lead into ``intent_id``, oldest first.

    Never raises: an unreadable or incomplete graph yields a short path or an empty one,
    because a missing narrative must not take the whole report down.
    """
    try:
        graph = load_graph(conn, task_id)
        return _build(graph, intent_id=intent_id, chain_id=chain_id, max_links=max_links)
    except Exception:  # noqa: BLE001 - a report must not fail on a lineage query
        return []


@dataclass(frozen=True, slots=True)
class _Graph:
    """One task's lineage, loaded once and shared across every Result in a listing."""

    rows: list
    by_id: dict
    sources: dict[str, list[str]]
    gains: dict[str, str]


def load_graph(conn: Connection, task_id: str) -> _Graph:
    rows = list(
        conn.execute(
            select(
                intents.c.id,
                intents.c.chain_id,
                intents.c.description,
                intents.c.to_fact_id,
                intents.c.created_at,
            )
            .where(intents.c.task_id == task_id)
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings()
    )
    return _Graph(
        rows=rows,
        by_id={row["id"]: row for row in rows},
        sources=_sources_by_intent(conn, task_id),
        gains=_gain_by_intent(conn, task_id),
    )


def attack_paths(
    conn: Connection,
    *,
    task_id: str,
    intents_with_results: Iterable[str],
    max_links: int = MAX_PATH_LINKS,
) -> dict[str, list[PathLink]]:
    """Bulk form: one graph load, one walk per Intent that has a verified Result.

    The console lists Results, and every row wants its own path; loading the graph per
    row would re-read the whole Intent table N times.
    """
    try:
        graph = load_graph(conn, task_id)
    except Exception:  # noqa: BLE001
        return {}
    return {
        intent_id: attack_paths_for_graph(graph, intent_id=intent_id, max_links=max_links)
        for intent_id in intents_with_results
    }


def attack_paths_for_graph(
    graph: _Graph,
    *,
    intent_id: str,
    chain_id: str | None = None,
    max_links: int = MAX_PATH_LINKS,
) -> list[PathLink]:
    """Same result as :func:`attack_path`, reusing an already-loaded graph."""
    try:
        return _build(graph, intent_id=intent_id, chain_id=chain_id, max_links=max_links)
    except Exception:  # noqa: BLE001
        return []


def _build(
    graph: _Graph,
    *,
    intent_id: str,
    chain_id: str | None,
    max_links: int = MAX_PATH_LINKS,
) -> list[PathLink]:
    rows = graph.rows
    if not rows:
        return []
    by_id = graph.by_id
    sources = graph.sources
    gains = graph.gains

    current = by_id.get(intent_id)
    if current is None:
        return []
    start_chain = (chain_id or current["chain_id"] or "").strip()

    # Walk the chain that produced the current Intent, then keep jumping to whatever
    # chain produced the facts this chain consumed, until nothing upstream is left.
    # Each segment is chronological within its own chain; segments are prepended, so
    # the final list reads oldest-first across chains.
    segments: list[list[PathLink]] = []
    visited_chains: set[str] = set()
    visited_facts: set[str] = set()
    active_chain = start_chain
    while active_chain and active_chain not in visited_chains:
        visited_chains.add(active_chain)
        chain_intents = [row for row in rows if (row["chain_id"] or "") == active_chain]
        if not chain_intents:
            break
        # The walk describes how the reported Result was reached, so it must stop at
        # that Intent: links created after it did not contribute to this Result, and
        # including them would make an early Result's report grow every time the chain
        # advances.
        cutoff = _position(chain_intents, intent_id)
        if cutoff is not None:
            chain_intents = chain_intents[: cutoff + 1]
        segments.append(
            [
                PathLink(
                    intent_id=row["id"],
                    chain_id=active_chain,
                    title=_title(row, sources.get(row["id"], [])),
                    gain=gains.get(row["id"], ""),
                    is_current=row["id"] == intent_id,
                )
                for row in chain_intents
            ]
        )
        upstream_chain = _upstream_chain(
            chain_intents,
            sources=sources,
            by_id=by_id,
            visited_facts=visited_facts,
        )
        if not upstream_chain or upstream_chain == active_chain:
            break
        active_chain = upstream_chain

    links: list[PathLink] = []
    for segment in reversed(segments):
        links.extend(segment)
    links = _with_current(links, current, start_chain, sources, gains)

    if len(links) > max_links:
        dropped = len(links) - max_links
        links = links[-max_links:]
        if links:
            head = links[0]
            links[0] = PathLink(
                intent_id=head.intent_id,
                chain_id=head.chain_id,
                title=_ELLIPSIS.format(count=dropped),
                gain="",
                is_current=False,
            )
    return links


def _position(chain_rows: list, intent_id: str) -> int | None:
    """Index of ``intent_id`` in this chain's chronological order, or None."""
    for index, row in enumerate(chain_rows):
        if row["id"] == intent_id:
            return index
    return None


def _with_current(
    links: list[PathLink],
    current,
    chain_id: str,
    sources: dict[str, list[str]],
    gains: dict[str, str],
) -> list[PathLink]:
    """Guarantee the reporting Intent appears, marked as the current link."""
    if current is None:
        return links
    for index, link in enumerate(links):
        if link.intent_id == current["id"]:
            links[index] = PathLink(
                intent_id=link.intent_id,
                chain_id=link.chain_id,
                title=link.title,
                gain=link.gain,
                is_current=True,
            )
            return links
    links.append(
        PathLink(
            intent_id=current["id"],
            chain_id=chain_id or (current["chain_id"] or ""),
            title=_title(current, sources.get(current["id"], [])),
            gain=gains.get(current["id"], ""),
            is_current=True,
        )
    )
    return links


def _upstream_chain(
    chain_intents: list,
    *,
    sources: dict[str, list[str]],
    by_id: dict,
    visited_facts: set[str],
) -> str:
    """Which chain produced the facts this chain's first Intent consumed."""
    for row in chain_intents:
        for fact_id in sources.get(row["id"], []):
            if fact_id in visited_facts:
                continue
            visited_facts.add(fact_id)
            producer = _producer_of(fact_id, by_id)
            if producer is None:
                continue
            producer_chain = (producer["chain_id"] or "").strip()
            if producer_chain:
                return producer_chain
    return ""


def _producer_of(fact_id: str, by_id: dict):
    """The Intent that produced ``fact_id``; earliest wins when several claim it."""
    candidates = [row for row in by_id.values() if (row["to_fact_id"] or "") == fact_id]
    if not candidates:
        return None
    return min(candidates, key=lambda row: (str(row["created_at"]), str(row["id"])))


def _sources_by_intent(conn: Connection, task_id: str) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for row in conn.execute(
        select(intent_sources.c.intent_id, intent_sources.c.fact_id).where(
            intent_sources.c.task_id == task_id
        )
    ):
        grouped.setdefault(row[0], []).append(row[1])
    return grouped


def _gain_by_intent(conn: Connection, task_id: str) -> dict[str, str]:
    """Each Intent's verified gain: what capability it actually added.

    Read from the Result's stored reproduction (``chain_gain``) because that is the
    worker's own one-line statement of the increment; the chain's
    ``current_state.capabilities`` would only give the cumulative set.
    """
    gains: dict[str, str] = {}
    for row in conn.execute(
        select(
            red_team_results.c.intent_id,
            red_team_results.c.title,
            red_team_results.c.reproduction_json,
        )
        .where(
            red_team_results.c.task_id == task_id,
            red_team_results.c.status == "verified",
        )
        .order_by(red_team_results.c.created_at)
    ).mappings():
        gain = _chain_gain(row.get("reproduction_json")) or (row.get("title") or "").strip()
        if gain and row["intent_id"] not in gains:
            gains[row["intent_id"]] = gain
    return gains


def _chain_gain(raw) -> str:
    import json

    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return ""
    else:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("chain_gain") or "").strip()


def _title(row, fact_ids: list[str]) -> str:
    """A short human line for one link.

    ``intents.description`` is a four-paragraph task brief; the report needs the first
    sentence of its goal, not the whole thing.
    """
    text = str(row["description"] or "").strip()
    goal = _goal_of(text) or text
    goal = " ".join(goal.split())
    if not goal and fact_ids:
        goal = "（消费 " + "、".join(fact_ids[:3]) + "）"
    if not goal:
        return "（无描述）"
    return goal[:120] + ("…" if len(goal) > 120 else "")


def _goal_of(text: str) -> str:
    """Extract the 目标 paragraph from Reason's four-part Intent brief."""
    marker = "目标："
    index = text.find(marker)
    if index < 0:
        marker = "目标:"
        index = text.find(marker)
    if index < 0:
        return ""
    body = text[index + len(marker):]
    for stop in ("判据：", "判据:", "前置：", "前置:", "边界：", "边界:", "\n"):
        cut = body.find(stop)
        if cut >= 0:
            body = body[:cut]
    return body.strip()
