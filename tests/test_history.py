import json

from app.history import DownloadHistory, DownloadRecord


def test_recover_incomplete_downloads(tmp_path):
    complete_path = tmp_path / "complete.bin"
    complete_path.write_bytes(b"abc")
    partial_path = tmp_path / "partial.bin"
    partial_path.write_bytes(b"a")
    reserved_path = tmp_path / "reserved.bin"
    reserved_path.touch()
    hidden_partial = tmp_path / ".reserved.bin.reserve1.part"
    hidden_partial.write_bytes(b"partial")
    history = DownloadHistory(tmp_path / "downloads.json")
    history.add(DownloadRecord("complete", 1, 1, complete_path.name, str(complete_path), status="downloading", total_bytes=3))
    history.add(DownloadRecord("partial", 2, 1, partial_path.name, str(partial_path), status="downloading", total_bytes=3))
    history.add(DownloadRecord("missing", 3, 1, "missing.bin", str(tmp_path / "missing.bin"), status="queued"))
    history.add(DownloadRecord("reserve1", 4, 1, reserved_path.name, str(reserved_path), status="downloading", total_bytes=10))

    result = history.recover_incomplete()

    assert result == {"recovered": 1, "interrupted": 3}
    assert history.find("complete")["status"] == "complete"
    assert history.find("partial")["status"] == "interrupted"
    assert history.find("missing")["status"] == "interrupted"
    assert not reserved_path.exists()
    assert not hidden_partial.exists()


def test_history_corruption_is_backed_up_and_service_can_continue(tmp_path):
    path = tmp_path / "downloads.json"
    path.write_text("{broken", encoding="utf-8")

    history = DownloadHistory(path)

    assert (tmp_path / "downloads.json.corrupt").exists()
    assert history.list()[0]["status"] == "failed"
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), list)
    assert not list(tmp_path.glob("*.tmp"))


def test_progress_updates_are_throttled_but_flush_persists_latest_state(tmp_path):
    path = tmp_path / "downloads.json"
    history = DownloadHistory(path, flush_interval=60)
    history.add(DownloadRecord("one", 1, 1, "one.bin", str(tmp_path / "one.bin")))

    history.update("one", persist=False, status="downloading", progress=55)
    before_flush = json.loads(path.read_text(encoding="utf-8"))[0]
    history.flush()
    after_flush = json.loads(path.read_text(encoding="utf-8"))[0]

    assert before_flush["progress"] == 0
    assert after_flush["progress"] == 55
