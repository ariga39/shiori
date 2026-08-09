import os
import sys
import uuid

import psycopg2
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import ingest

# Independent, known-good 1024-dim embedding vector (matches voyage-4-large).
VALID_EMB = [0.01] * 1024
# A wrong-dimension vector that fails the vector(1024) cast on INSERT.
WRONG_EMB = [0.0, 0.0]


def _connect():
    creds = ingest.load_credentials()
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["dbname"],
        user=creds["user"],
        password=creds["password"],
    )


@pytest.fixture
def emb():
    return VALID_EMB


@pytest.fixture
def wrong_emb():
    return WRONG_EMB


@pytest.fixture
def db_unused():
    """No-op placeholder for tests that must NOT connect to the DB."""
    return None


@pytest.fixture
def db():
    """Real DB connection + a unique test session prefix. Cleans up after.

    All tests allocate rows under the reserved `test-` session_id namespace. At
    setup we wipe that namespace so any rows left behind by a prior interrupted
    run (whose teardown never executed) cannot survive into this run. Without
    this, a leftover row whose content/vector is identical to a freshly-inserted
    test row gets collapsed by query MMR (threshold 0.85), so a recall test's
    `len(mine) == 2` assertion intermittently sees only 1 row (NB-C7-01).
    Wiping the reserved namespace at setup guarantees each test sees only its own
    rows, independently of the ~20k-row live table's HNSW ef_search setting.
    """
    conn = _connect()
    session_prefix = "test-%s" % uuid.uuid4().hex
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM session_chunks WHERE session_id LIKE 'test-%'")
        cur.execute("DELETE FROM ingestion_state WHERE file_path LIKE 'test-%'")
        conn.commit()
        cur.close()
        yield conn, session_prefix
    finally:
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM session_chunks WHERE session_id LIKE %s",
                (session_prefix + "%",),
            )
            cur.execute(
                "DELETE FROM ingestion_state WHERE file_path LIKE %s",
                (session_prefix + "%",),
            )
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
        finally:
            try:
                conn.close()
            except Exception:
                pass
