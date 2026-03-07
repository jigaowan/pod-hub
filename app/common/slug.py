from __future__ import annotations

import re


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    normalized = _SLUG_RE.sub("-", value.lower()).strip("-")
    return normalized or "channel"


def unique_slug(base_slug: str, existing_slugs: set[str]) -> str:
    if base_slug not in existing_slugs:
        return base_slug

    suffix = 2
    while True:
        candidate = f"{base_slug}-{suffix}"
        if candidate not in existing_slugs:
            return candidate
        suffix += 1

