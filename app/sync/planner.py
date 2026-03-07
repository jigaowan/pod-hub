from __future__ import annotations

from app.db.models.episode import Episode


def plan_initial_fetches(candidates: list[Episode], batch_size: int) -> list[Episode]:
    return candidates[:batch_size]


def plan_polling_fetches(
    *,
    new_candidates: list[Episode],
    backlog_candidates: list[Episode],
    batch_size: int,
) -> list[Episode]:
    prioritized_new = new_candidates[:batch_size]
    remaining_slots = max(batch_size - len(prioritized_new), 0)
    return [*prioritized_new, *backlog_candidates[:remaining_slots]]
