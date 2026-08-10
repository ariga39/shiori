import os
import sys
import uuid

import psycopg2
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# Test databases are opt-in and isolated.  Never fall back to a normal-user
# credential file or a shared production DSN.  CI creates a random database
# and marker table, then supplies both values below.
_TEST_DSN = os.environ.get("SHIORI_TEST_DATABASE_DSN")
_TEST_DB_NAME = os.environ.get("SHIORI_TEST_DATABASE_NAME")
_TEST_DB_MARKER = os.environ.get("SHIORI_TEST_DATABASE_MARKER")
if _TEST_DSN:
    # Existing legacy tests call ingest.load_credentials directly.  Make that
    # explicit test DSN visible to the application without allowing ambient
    # home credentials to participate.
    os.environ["SHIORI_DATABASE_DSN"] = _TEST_DSN

# Independent, known-good 1024-dim embedding vector (matches voyage-4-large).
VALID_EMB = [0.01] * 1024
# A wrong-dimension vector that fails the vector(1024) cast on INSERT.
WRONG_EMB = [0.0, 0.0]


def _connect():
    if not (_TEST_DSN and _TEST_DB_NAME and _TEST_DB_MARKER):
        pytest.skip(
            "isolated PostgreSQL not configured; set SHIORI_TEST_DATABASE_DSN, "
            "SHIORI_TEST_DATABASE_NAME, and SHIORI_TEST_DATABASE_MARKER"
        )
    conn = psycopg2.connect(_TEST_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            database_name = cur.fetchone()[0]
            cur.execute("SELECT marker FROM shiori_test_guard")
            marker = cur.fetchone()[0]
    except Exception:
        conn.close()
        raise
    if database_name != _TEST_DB_NAME or marker != _TEST_DB_MARKER:
        conn.close()
        raise RuntimeError("refusing to use a non-matching isolated shiori test database")
    return conn


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

    All tests allocate rows under a unique `test-<uuid>` session_id namespace.
    The fixture never wipes a shared `test-%` prefix: that could delete another
    job's rows when an operator intentionally points two test processes at the
    same reserved database. The CI contract supplies a fresh random database;
    interrupted local rows remain isolated by their unique prefix.
    """
    conn = _connect()
    session_prefix = f"test-{uuid.uuid4().hex}"
    try:
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
