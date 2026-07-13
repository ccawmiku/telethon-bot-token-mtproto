import json
import os
import stat

from app.config import Settings, SettingsStore, verify_password


def make_settings(tmp_path):
    return Settings.from_json_dict(
        {
            "download_dir": str(tmp_path / "downloads"),
            "image_download_dir": str(tmp_path / "downloads" / "images"),
            "video_download_dir": str(tmp_path / "downloads" / "videos"),
            "file_download_dir": str(tmp_path / "downloads" / "files"),
            "session_dir": str(tmp_path / "sessions"),
            "config_dir": str(tmp_path / "config"),
        }
    )


def test_settings_are_written_atomically_with_private_permissions(tmp_path):
    store = SettingsStore(make_settings(tmp_path))

    saved = store.save({"allowed_user_ids": "123", "admin_password": "strong-password"})
    data = json.loads(saved.config_path.read_text(encoding="utf-8"))

    assert data["allowed_user_ids"] == [123]
    assert verify_password("strong-password", data["admin_password_hash"])
    assert not list(saved.config_dir.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(saved.config_path.stat().st_mode) == 0o600


def test_corrupt_settings_are_backed_up_and_environment_seed_survives(tmp_path):
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True)
    settings.config_path.write_text("{broken", encoding="utf-8")

    store = SettingsStore(settings)

    assert store.settings.download_dir == settings.download_dir
    assert settings.config_path.with_suffix(".json.corrupt").exists()
