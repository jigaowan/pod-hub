from __future__ import annotations

import errno
import os
import shutil
import tempfile
from hashlib import sha256
from pathlib import Path

from podhub.storage.paths import safe_path, source_dir


STAGING_PREFIX = ".podhub-publish-"


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


HARDLINK_FALLBACK_ERRNOS = frozenset(
    code for name in ("EXDEV", "EPERM", "ENOTSUP", "EOPNOTSUPP", "EMLINK", "ENOSYS")
    if (code := getattr(errno, name, None)) is not None
)


def atomic_publish(source: Path, target: Path, *, hardlink: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if hardlink:
            os.link(source, target)
        else:
            source.chmod(0o644)
            with source.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(source, target)
    except FileExistsError:
        if not hardlink:
            raise
        # A previous publish may have stopped before committing SQLite.
        if source.samefile(target):
            return
        atomic_copy(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV and not (hardlink and exc.errno in HARDLINK_FALLBACK_ERRNOS):
            raise
        atomic_copy(source, target)
    sync_directory(target.parent)


def atomic_copy(source: Path, target: Path) -> None:
    fd, name = tempfile.mkstemp(prefix=STAGING_PREFIX, dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as dest, source.open("rb") as origin:
            shutil.copyfileobj(origin, dest)
            os.fchmod(dest.fileno(), 0o644)
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def save_cover(artwork: Path, root: Path, slug: str) -> str:
    name = f"cover.{sha256(artwork.read_bytes()).hexdigest()[:24]}.jpg"
    atomic_publish(artwork, safe_path(source_dir(root, slug), name))
    return name
