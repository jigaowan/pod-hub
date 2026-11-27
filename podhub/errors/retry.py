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
    *, release: int | None = None, live_status: str | None = None,
    now: datetime | None = None, alternate_source: bool = False,
) -> RetryDecision:
    now = now or utc_now()
    category = failure.category
    counts = json.loads(video["category_attempts_json"])
    count = counts.get(category, 0) + 1
    in_soft_recheck = video["last_category"] == Category.SOFT_UNAVAILABLE and video["state"] in ("unavailable", "running")
    state = VideoState.PENDING
    delay: float | None = None
    charged = False
    if category in (Category.RATE_LIMIT_OR_BOT, Category.TOOLCHAIN_BROKEN, Category.COOKIE_INVALID, Category.LOCAL_RESOURCE):
        if in_soft_recheck:
            state = VideoState.UNAVAILABLE
            delay = 1800
    elif category in (Category.PERMANENT_UNAVAILABLE, Category.AUTH_REQUIRED):
        state = VideoState.PENDING if category == Category.AUTH_REQUIRED and alternate_source else VideoState.UNAVAILABLE
    else:
        counts[category] = count
        if category == Category.NOT_READY:
            state = VideoState.DEFERRED
            future_release = release is not None and live_status not in ("is_live", "post_live") and (release + 900 - now.timestamp()) > 60
            delay = (release + 900 - now.timestamp()) if future_release else (1800, 3600, 7200)[min(count - 1, 2)]
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
                state = VideoState.UNAVAILABLE if in_soft_recheck else VideoState.FAILED
            else:
                state = VideoState.UNAVAILABLE if in_soft_recheck else VideoState.PENDING
                delay = schedule[count - 1] * random.uniform(0.8, 1.2)
    due = stamp(now, seconds=delay) if delay is not None else None
    deadline = video["give_up_at"]
    if deadline and category != Category.SOFT_UNAVAILABLE and not in_soft_recheck:
        if stamp(now) >= deadline:
            state = VideoState.UNAVAILABLE if category == Category.NOT_READY else VideoState.FAILED
            due = None
        elif due and due > deadline:
            due = deadline
    return RetryDecision(state, due, counts, category == Category.UNCLASSIFIED, charged)
