from __future__ import annotations

import os
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as dist_version


@lru_cache(maxsize=1)
def app_version() -> str:
    if build := os.environ.get("PODHUB_BUILD_VERSION", "").strip():
        return build
    try:
        return f"v{dist_version('pod-hub')}"
    except PackageNotFoundError:
        return "dev"
