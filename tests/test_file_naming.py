from pathlib import Path

from app.config import Settings, hash_password, parse_user_ids, verify_password
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


def test_settings_routes_images_and_videos_to_separate_directories():
    settings = Settings.from_json_dict(
        {
            "download_dir": "/downloads",
            "image_download_dir": "/downloads/images",
            "video_download_dir": "/downloads/videos",
            "file_download_dir": "/downloads/files",
        }
    )

    assert settings.media_dir("photo") == Path("/downloads/images").resolve()
    assert settings.media_dir("image") == Path("/downloads/images").resolve()
    assert settings.media_dir("video") == Path("/downloads/videos").resolve()
    assert settings.media_dir("file") == Path("/downloads/files").resolve()


def test_parse_user_ids_accepts_commas_spaces_and_newlines():
    assert parse_user_ids("123, 456\n789 123") == [123, 456, 789]


def test_password_hash_verification():
    password_hash = hash_password("secret-password")

    assert verify_password("secret-password", password_hash)
    assert not verify_password("wrong-password", password_hash)
