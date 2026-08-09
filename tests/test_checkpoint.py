from datetime import datetime, timezone

import ingest

TS = datetime(2026, 8, 3, 1, 0, 0, tzinfo=timezone.utc)


def test_mark_records_real_size(db):
    conn, prefix = db
    path = "%s/full.jsonl" % prefix
    ingest.mark_file_processed(conn, path, TS, 100, "main_user", 5, partial=False)
    proc = ingest.get_processed_files(conn)
    assert proc[path]["size"] == 100
    assert proc[path]["mtime"] == TS


def test_mark_partial_forces_retry_with_size_zero(db):
    conn, prefix = db
    path = "%s/partial.jsonl" % prefix
    ingest.mark_file_processed(conn, path, TS, 100, "main_user", 2, partial=False)
    proc = ingest.get_processed_files(conn)
    assert proc[path]["size"] == 100

    # partial=True → file_size forced to 0 so the next run re-processes it.
    ingest.mark_file_processed(conn, path, TS, 100, "main_user", 2, partial=True)
    proc = ingest.get_processed_files(conn)
    assert proc[path]["size"] == 0


def test_remark_unchanged_is_idempotent(db):
    conn, prefix = db
    path = "%s/idem.jsonl" % prefix
    # mark as fully processed (mtime + size recorded).
    ingest.mark_file_processed(conn, path, TS, 500, "main_user", 9, partial=False)
    proc = ingest.get_processed_files(conn)
    assert (proc[path]["mtime"], proc[path]["size"]) == (TS, 500)

    # Re-marking with the same mtime+size (unchanged file) keeps the same
    # record — this is exactly the checkpoint main() reads to skip reprocessing.
    ingest.mark_file_processed(conn, path, TS, 500, "main_user", 9, partial=False)
    proc = ingest.get_processed_files(conn)
    assert (proc[path]["mtime"], proc[path]["size"]) == (TS, 500)


def test_full_mark_enables_skip_on_unchanged(db):
    conn, prefix = db
    path = "%s/skip.jsonl" % prefix
    # Full successful ingest → file_size == real size.
    ingest.mark_file_processed(conn, path, TS, 321, "main_user", 4, partial=False)
    proc = ingest.get_processed_files(conn)

    # main() skips a file when prev mtime+size both match the on-disk stat.
    assert proc[path]["mtime"] == TS
    assert proc[path]["size"] == 321
