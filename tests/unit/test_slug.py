from app.common.slug import slugify, unique_slug


def test_slugify_normalizes_text() -> None:
    assert slugify("Hello Pod Hub!") == "hello-pod-hub"


def test_slugify_preserves_non_ascii_letters() -> None:
    assert slugify("播客清单 01") == "播客清单-01"


def test_slugify_uses_fallback_for_empty_slug() -> None:
    assert slugify("!!!", fallback="PL123") == "pl123"


def test_unique_slug_appends_suffix() -> None:
    assert unique_slug("demo", {"demo", "demo-2"}) == "demo-3"
