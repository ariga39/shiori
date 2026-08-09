

import ingest
import ingest_discord


def _run(module, argv, monkeypatch, tmp_path, get_db_raises=True):
    monkeypatch.setattr("sys.argv", argv)
    if get_db_raises:
        monkeypatch.setattr(module, "get_db", _boom)

    if module is ingest:
        monkeypatch.setattr(ingest, "find_session_files", lambda: [])
    else:
        empty_dir = tmp_path / "archive"
        empty_dir.mkdir()
        monkeypatch.setattr(ingest_discord, "ARCHIVE_DIR", empty_dir)
    # module under test
    module.main()


def _boom(*a, **k):
    raise AssertionError("get_db must not be called during --dry-run")


def test_ingest_dry_run_does_not_connect(db_unused, monkeypatch, tmp_path):
    _run(ingest, ["ingest.py", "--dry-run"], monkeypatch, tmp_path)


def test_ingest_dry_run_force_does_not_connect(db_unused, monkeypatch, tmp_path):
    _run(ingest, ["ingest.py", "--dry-run", "--force"], monkeypatch, tmp_path)


def test_discord_dry_run_does_not_connect(db_unused, monkeypatch, tmp_path):
    _run(ingest_discord, ["ingest_discord.py", "--dry-run"], monkeypatch, tmp_path)


def test_discord_dry_run_force_does_not_connect(db_unused, monkeypatch, tmp_path):
    _run(ingest_discord, ["ingest_discord.py", "--dry-run", "--force"], monkeypatch, tmp_path)
