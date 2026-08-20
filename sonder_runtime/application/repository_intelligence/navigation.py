"""Provider-neutral repository navigation evidence and bounded expansion."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NavigationEvidence:
    root_id: str
    file_path: str
    symbol: str
    relation: str
    source: str = "index"
    revision: str = ""


@dataclass(frozen=True)
class ExpansionRequest:
    root_ids: tuple[str, ...]
    seed_symbols: tuple[str, ...]
    max_symbols: int = 100
    max_hops: int = 2

    def __post_init__(self) -> None:
        if not self.root_ids or not self.seed_symbols or not 1 <= self.max_symbols <= 10_000 or not 0 <= self.max_hops <= 10:
            raise ValueError("invalid bounded expansion request")


def expand(evidence: tuple[NavigationEvidence, ...], request: ExpansionRequest) -> tuple[NavigationEvidence, ...]:
    roots = set(request.root_ids)
    seeds = set(request.seed_symbols)
    selected = []
    frontier = set(seeds)
    for hop in range(request.max_hops + 1):
        if not frontier or len(selected) >= request.max_symbols:
            break
        next_frontier: set[str] = set()
        for item in evidence:
            if item.root_id in roots and item.symbol in frontier and item not in selected:
                selected.append(item)
                next_frontier.add(item.relation)
                if len(selected) >= request.max_symbols:
                    break
        frontier = next_frontier
    return tuple(selected)
