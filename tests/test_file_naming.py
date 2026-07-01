from pathlib import Path

from app.file_naming import sanitize_filename


def test_sanitize_filename_removes_unsafe_characters():
    assert sanitize_filename("../bad:name?.jpg", "fallback.jpg", 120) == "bad_name_.jpg"


def test_sanitize_filename_uses_fallback_for_empty_name():
    assert sanitize_filename("////", "photo.jpg", 120) == "photo.jpg"


def test_sanitize_filename_truncates_stem_but_keeps_suffix():
    result = sanitize_filename("a" * 200 + ".mp4", "video.mp4", 12)
    assert result == "a" * 12 + ".mp4"


def test_path_suffix_behavior_matches_windows_safe_names():
    assert Path(sanitize_filename("clip final.mp4", "video.mp4", 120)).suffix == ".mp4"
