from __future__ import annotations

from collections.abc import Iterator

from podhub.postprocess.base import PostProcessor


class PostProcessorRegistry:
    def __init__(self) -> None:
        self._processors: dict[str, PostProcessor] = {}

    def register(self, processor: PostProcessor) -> None:
        if not processor.name or processor.name in self._processors:
            raise ValueError(f"invalid or duplicate processor name: {processor.name}")
        self._processors[processor.name] = processor

    def get(self, name: str) -> PostProcessor | None:
        return self._processors.get(name)

    def __iter__(self) -> Iterator[PostProcessor]:
        return iter(self._processors.values())

    def __bool__(self) -> bool:
        return bool(self._processors)
