from __future__ import annotations

import unicodedata


def slugify(value: str, *, fallback: str = "channel") -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    parts: list[str] = []
    previous_was_separator = False

    for char in normalized:
        if char.isalnum():
            parts.append(char)
            previous_was_separator = False
            continue

        if previous_was_separator:
            continue

        parts.append("-")
        previous_was_separator = True

    slug = "".join(parts).strip("-")
    if slug:
        return slug

    fallback_normalized = unicodedata.normalize("NFKC", fallback).strip().lower()
    fallback_slug = "".join(char for char in fallback_normalized if char.isalnum() or char == "-").strip("-")
    return fallback_slug or "channel"


def unique_slug(base_slug: str, existing_slugs: set[str]) -> str:
    if base_slug not in existing_slugs:
        return base_slug

    suffix = 2
    while True:
        candidate = f"{base_slug}-{suffix}"
        if candidate not in existing_slugs:
            return candidate
        suffix += 1
