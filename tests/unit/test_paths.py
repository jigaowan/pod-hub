from __future__ import annotations

import pytest

from podhub.storage.paths import default_source_slug, sanitize_folder_name, source_dir, validate_folder_name


@pytest.mark.parametrize(("title", "original", "external_id", "expected"), [
    ("  Café　 中文  ", "@My_Handle", "UCFallback", "Café 中文"),
    ("放送局", "youtube.com/@Radio_Tokyo/videos", "UCFallback", "放送局"),
    ("?*", "@My_Handle", "UCFallback", "My_Handle"),
    ("/", "https://www.youtube.com/@%E5%8F%B0%E5%8C%97/videos", "UCFallback", "台北"),
    ("...", "@?", "UC_AbCD", "UC_AbCD"),
    ("***", "https://www.youtube.com/playlist?list=PL_AbCD", "PL_AbCD", "PL_AbCD"),
])
def test_default_source_folder_slug(title, original, external_id, expected):
    assert default_source_slug(title, original, external_id) == expected


def test_folder_validation_normalizes_unicode_and_whitespace():
    raw = "  Cafe\u0301　中国  か\u3099く  "
    assert validate_folder_name(raw) == "Café 中国 がく"
    assert validate_folder_name("音楽 ＆ 家庭👨‍👩‍👧‍👦") == "音楽 ＆ 家庭👨‍👩‍👧‍👦"


@pytest.mark.parametrize("name", [
    "", "　 ", ".", "..", "　.目录", ".podhub-publish-stage", "中国/台湾", r"日本\東京",
    "文件\x00名", "文件\x7f名", "文件\x85名", "文件\t名", "文件\ud800名",
    "名:称", "節目?", "番組.", "CON", "nul", "PrN.txt", "COM¹", "LPT9", "NUL .txt",
])
def test_explicit_folder_rejects_invalid_names(name):
    with pytest.raises(ValueError, match="folder name"):
        validate_folder_name(name)


def test_folder_byte_limit_and_sanitization():
    limit = "聲" * 40
    assert validate_folder_name(limit) == limit
    with pytest.raises(ValueError, match="120 bytes"):
        validate_folder_name(f"{limit}a")
    assert sanitize_folder_name(f"{limit}音") == limit
    assert sanitize_folder_name("  .Cafe\u0301　:回/声?  ") == "Café 回声"
    assert sanitize_folder_name("番組\x85?\x00") == "番組"
    assert sanitize_folder_name("日本\t　放送") == "日本 放送"
    assert sanitize_folder_name("PRN") == "PRN_"
    assert sanitize_folder_name("..") == ""


def test_source_directory_rejects_symlinks(tmp_path):
    target = tmp_path / "対象"
    target.mkdir()
    (tmp_path / "別名").symlink_to(target, target_is_directory=True)
    assert source_dir(tmp_path, "  対象  ") == target
    with pytest.raises(ValueError, match="symlinks"):
        source_dir(tmp_path, "別名")
