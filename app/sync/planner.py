from __future__ import annotations

from app.db.models.episode import Episode


def plan_initial_fetches(candidates: list[Episode], batch_size: int) -> list[Episode]:
    return candidates[:batch_size]


def plan_polling_fetches(
    *,
    new_candidates: list[Episode],
    backlog_candidates: list[Episode],
    backlog_batch_size: int,
) -> list[Episode]:
    return [*new_candidates, *backlog_candidates[:backlog_batch_size]]

