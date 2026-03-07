from app.common.slug import slugify, unique_slug


def test_slugify_normalizes_text() -> None:
    assert slugify("Hello Pod Hub!") == "hello-pod-hub"


def test_unique_slug_appends_suffix() -> None:
    assert unique_slug("demo", {"demo", "demo-2"}) == "demo-3"

