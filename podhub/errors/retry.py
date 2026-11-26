from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from podhub.config import Retry
from podhub.domain.enums import Category, VideoState
from podhub.domain.models import stamp, utc_now
from podhub.errors.taxonomy import Classification


@dataclass(frozen=True)
class RetryDecision:
    state: VideoState
    next_attempt_at: str | None
    category_attempts: dict[str, int]
    unclassified: bool
    charge_attempt: bool = False


def plan_retry(
    retry: Retry, video: sqlite3.Row, failure: Classification,
    *, release: int | None = None, now: datetime | None = None, alternate_source: bool = False,
) -> RetryDecision:
    now = now or utc_now()
    category = failure.category
    counts = json.loads(video["category_attempts_json"])
    count = counts.get(category, 0) + 1
    state = VideoState.PENDING
    delay: float | None = None
    charged = False
    if category in (Category.RATE_LIMIT_OR_BOT, Category.TOOLCHAIN_BROKEN, Category.COOKIE_INVALID, Category.LOCAL_RESOURCE):
        pass
    elif category in (Category.PERMANENT_UNAVAILABLE, Category.AUTH_REQUIRED):
        state = VideoState.PENDING if category == Category.AUTH_REQUIRED and alternate_source else VideoState.UNAVAILABLE
    else:
        counts[category] = count
        if category == Category.NOT_READY:
            state = VideoState.DEFERRED
            delay = max(60, release + 900 - now.timestamp()) if release else (1800, 3600, 7200)[min(count - 1, 2)]
        elif category == Category.SOFT_UNAVAILABLE:
            state = VideoState.UNAVAILABLE
            if count <= len(retry.soft_unavailable_recheck_days):
                delay = retry.soft_unavailable_recheck_days[count - 1] * 86400
        else:
            charged = True
            schedule = {
                Category.TRANSIENT_NETWORK: retry.transient_backoff_s,
                Category.DOWNLOAD_403: retry.download_403_backoff_s,
                Category.CORRUPTED_ARTIFACT: [600],
            }.get(category, retry.unclassified_backoff_s)
            if count > len(schedule):
                state = VideoState.FAILED
            else:
                delay = schedule[count - 1] * random.uniform(0.8, 1.2)
    due = stamp(now, seconds=delay) if delay is not None else None
    deadline = video["give_up_at"]
    if deadline and category != Category.SOFT_UNAVAILABLE:
        if stamp(now) >= deadline:
            state = VideoState.UNAVAILABLE if category == Category.NOT_READY else VideoState.FAILED
            due = None
        elif due and due > deadline:
            due = deadline
    return RetryDecision(state, due, counts, category == Category.UNCLASSIFIED, charged)
