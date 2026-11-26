from __future__ import annotations

import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

from podhub.domain.models import parse_date
from podhub.storage.media import sync_directory
from podhub.storage.paths import safe_path


def expiry(contents: str) -> str | None:
    if len(contents.encode()) > 2_000_000:
        raise ValueError("cookie file exceeds 2 MB")
    expiries = []
    cookie_count = 0
    for line in contents.splitlines():
        line = line.removeprefix("#HttpOnly_")
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) != 7 or not columns[4].isdigit():
            raise ValueError("expected a Netscape cookie file")
        cookie_count += 1
        if int(columns[4]):
            parsed = parse_date(int(columns[4]))
            if parsed:
                expiries.append(parsed)
    if not cookie_count:
        raise ValueError("cookie file contains no cookies")
    return min(expiries, default=None)


def save_cookie(directory: Path, name: str, contents: str) -> None:
    target = safe_path(directory, name)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as file:
        temp = Path(file.name)
        try:
            os.fchmod(file.fileno(), 0o600)
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
            os.replace(temp, target)
            sync_directory(directory)
        finally:
            temp.unlink(missing_ok=True)


def copy_cookie(directory: Path, name: str, workspace: Path) -> Path:
    target = workspace / "cookies.txt"
    with safe_path(directory, name).open("rb") as origin:
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dest:
            shutil.copyfileobj(origin, dest)
    return target
