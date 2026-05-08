from __future__ import annotations

from app.common.errors import ValidationError

COOKIE_FILE_HEADERS = {"# HTTP Cookie File", "# Netscape HTTP Cookie File"}


def normalize_cookie_text(cookie_text: str | None) -> str | None:
    if cookie_text is None:
        return None

    normalized = cookie_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None

    first_line = normalized.split("\n", 1)[0].strip()
    if first_line not in COOKIE_FILE_HEADERS:
        raise ValidationError(
            "Cookie must be a Netscape/Mozilla cookies.txt file starting with "
            "# HTTP Cookie File or # Netscape HTTP Cookie File"
        )

    return f"{normalized}\n"
