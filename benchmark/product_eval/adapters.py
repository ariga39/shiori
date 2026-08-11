"""License-checked local-only dataset adapters for Phase 4D (task #18).

Each adapter exposes a frozen contract from the committed dataset_manifest.json
and a runnable LOCAL-PATH-ONLY parser. NO adapter downloads data, opens a URL,
loads a model, reads a credential, or opens a network socket. Public-dataset
raw/derived rows are parsed in memory or from an explicitly ignored temporary
directory and are NEVER committed; the manifest records the card license,
upstream provenance, and redistribution=unresolved/not_run for the unclosed
chains.

The parsers validate the frozen contract (revision/hash/MD5/fields) against
user-supplied local files, then yield parsed rows without persisting them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# model/network/credential-free dependency guard: importing this module must
# never pull in requests/urllib/sentence-transformers/torch.
_NO_NETWORK_IMPORTS = ("requests", "urllib.request", "sentence_transformers", "torch", "transformers")


class AdapterError(ValueError):
    """Raised when an adapter contract is violated."""


def load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AdapterError(message)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json_array_bytes(data: bytes) -> list[dict]:
    rows: list[dict] = []
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _read_qrels_bytes(data: bytes) -> list[dict]:
    qrels: list[dict] = []
    for line in data.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) != 3:
            raise AdapterError("NFCorpus qrels malformed line")
        qrels.append({"query_id": parts[0], "corpus_id": parts[1], "score": int(parts[2])})
    return qrels


def longmemeval_contract(manifest: dict) -> dict:
    """Frozen LongMemEval contract: local-only user-supplied, unresolved
    redistribution. Never download; never commit raw/derived rows."""
    spec = manifest["adapters"]["longmemeval"]
    _require(spec["status"] == "local_only", "LongMemEval must be local_only")
    _require(spec["redistribution"] == "unresolved", "LongMemEval redistribution must be unresolved")
    _require(len(spec["revision"]) == 40, "LongMemEval revision must be a full 40-char commit")
    _require(spec["hf_repo"] == "xiaowu0162/longmemeval-cleaned", "unexpected LongMemEval repo")
    for entry in spec["files"]:
        _require(entry.get("sha256"), f"LongMemEval file {entry['path']} missing pinned sha256")
        _require(len(entry["sha256"]) == 64, f"LongMemEval file {entry['path']} sha256 not 64 hex")
        _require(isinstance(entry["bytes"], int) and entry["bytes"] > 0, f"bad LongMemEval bytes {entry}")
    return spec


def longmemeval_parse(data_dir: Path, manifest: dict, *, sha256: dict[str, str] | None = None) -> dict:
    """Parse a LOCAL LongMemEval dataset directory (official format).

    Each manifest file is a JSON ARRAY of evaluation instances. Per the
    official Dataset Format each instance carries:
      question_id, question_type, question, answer, question_date,
      haystack_session_ids (list), haystack_dates (list),
      haystack_sessions (list of sessions; each session is a list of turns
      {"role","content"}; evidence turns add "has_answer": true),
      answer_session_ids (list of evidence session ids).
    Abstention is expressed by a question_id ending in "_abs" (no separate
    top-level has_answer field). Rows are returned in memory only.

    The parser verifies byte size + pinned sha256 (REQUIRED from the committed
    manifest) and the retrieval-relevant field contract. Raises AdapterError on
    any violation; errors never leak local paths.
    """
    spec = longmemeval_contract(manifest)
    parsed: dict[str, list[dict]] = {}
    for entry in spec["files"]:
        rel = entry["path"]
        expected_bytes = entry["bytes"]
        pinned = entry["sha256"]
        path = data_dir / rel
        if not path.is_file():
            raise AdapterError(f"LongMemEval missing local file: {rel}")
        actual = path.stat().st_size
        if actual != expected_bytes:
            raise AdapterError(f"LongMemEval {rel} byte mismatch: got {actual} expected {expected_bytes}")
        got = _sha256(path)
        if got != pinned:
            raise AdapterError(f"LongMemEval {rel} sha256 mismatch")
        if sha256 is not None and rel in sha256 and sha256[rel] != pinned:
            raise AdapterError(f"LongMemEval {rel} caller sha256 conflicts with pinned hash")
        instances = _read_json_array(path)
        _validate_longmemeval_instances(rel, instances)
        parsed[rel] = instances
    return parsed


def _read_json_array(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise AdapterError("expected a JSON array of instances")
    return data


_LONGMEMEVAL_QUESTION_TYPES = frozenset(
    {
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "temporal-reasoning",
        "knowledge-update",
        "multi-session",
    }
)
_LONGMEMEVAL_EXPECTED_INSTANCES = 500


def _validate_longmemeval_instances(rel: str, instances: list[dict]) -> None:
    if len(instances) != _LONGMEMEVAL_EXPECTED_INSTANCES:
        raise AdapterError(
            f"LongMemEval {rel} must contain exactly {_LONGMEMEVAL_EXPECTED_INSTANCES} instances, "
            f"got {len(instances)}"
        )
    seen_qids: set[str] = set()
    for inst in instances:
        for field in (
            "question_id", "question_type", "question", "answer",
            "question_date", "haystack_session_ids", "haystack_dates",
            "haystack_sessions", "answer_session_ids",
        ):
            if field not in inst:
                raise AdapterError(f"LongMemEval {rel} instance missing field {field!r}")
        qid = inst["question_id"]
        if not isinstance(qid, str) or not qid:
            raise AdapterError(f"LongMemEval {rel} question_id must be a non-empty string")
        if qid in seen_qids:
            raise AdapterError(f"LongMemEval {rel} duplicate question_id: {qid}")
        seen_qids.add(qid)
        if inst["question_type"] not in _LONGMEMEVAL_QUESTION_TYPES:
            raise AdapterError(f"LongMemEval {rel} invalid question_type: {inst['question_type']!r}")
        for field in ("question", "answer", "question_date"):
            if not isinstance(inst[field], str) or not inst[field]:
                raise AdapterError(f"LongMemEval {rel} {field} must be a non-empty string")
        for key in ("haystack_session_ids", "haystack_dates", "answer_session_ids"):
            if not isinstance(inst[key], list):
                raise AdapterError(f"LongMemEval {rel} {key} must be a list")
        if len(inst["haystack_session_ids"]) != len(inst["haystack_dates"]):
            raise AdapterError(f"LongMemEval {rel} haystack_session_ids/dates length mismatch")
        if not isinstance(inst["haystack_sessions"], list):
            raise AdapterError(f"LongMemEval {rel} haystack_sessions must be a list")
        if len(inst["haystack_session_ids"]) != len(inst["haystack_sessions"]):
            raise AdapterError(
                f"LongMemEval {rel} haystack_session_ids/dates/sessions must be equal length"
            )
        session_ids = set(inst["haystack_session_ids"])
        if len(session_ids) != len(inst["haystack_session_ids"]):
            raise AdapterError(f"LongMemEval {rel} haystack_session_ids must be unique")
        for session_id in inst["answer_session_ids"]:
            if session_id not in session_ids:
                raise AdapterError(f"LongMemEval {rel} answer_session_ids not in haystack_session_ids")

        evidence_session_ids: set[str] = set()
        for idx, session in enumerate(inst["haystack_sessions"]):
            if not isinstance(session, list):
                raise AdapterError(f"LongMemEval {rel} each haystack session must be a list of turns")
            for turn in session:
                if not isinstance(turn, dict) or "role" not in turn or "content" not in turn:
                    raise AdapterError(f"LongMemEval {rel} turn must have role/content")
                if turn["role"] not in ("user", "assistant"):
                    raise AdapterError(f"LongMemEval {rel} turn role must be user|assistant")
                if not isinstance(turn["content"], str):
                    raise AdapterError(f"LongMemEval {rel} turn content must be a string")
                if turn.get("has_answer") is True:
                    evidence_session_ids.add(inst["haystack_session_ids"][idx])
                elif turn.get("has_answer") not in (None, True):
                    raise AdapterError(f"LongMemEval {rel} turn has_answer must be true or absent")

        # Abstention semantics: question_id ending _abs is abstention; it must
        # have empty answer_session_ids AND no evidence turns. Non-abstention
        # must have answer_session_ids AND the evidence-turn session set exactly
        # equal to answer_session_ids.
        is_abs = qid.endswith("_abs")
        if is_abs:
            if inst["answer_session_ids"]:
                raise AdapterError(f"LongMemEval {rel} abstention instance must have empty answer_session_ids")
            if evidence_session_ids:
                raise AdapterError(f"LongMemEval {rel} abstention instance must not contain evidence turns")
        else:
            if not inst["answer_session_ids"]:
                raise AdapterError(f"LongMemEval {rel} non-abstention instance must have answer_session_ids")
            if evidence_session_ids != set(inst["answer_session_ids"]):
                raise AdapterError(
                    f"LongMemEval {rel} evidence-turn sessions must equal answer_session_ids"
                )


def nfcorpus_contract(manifest: dict) -> dict:
    """Frozen NFCorpus contract: local-only, official-archive MD5 pinned,
    unresolved qrels redistribution. The first PR never commits the archive."""
    spec = manifest["adapters"]["nfcorpus"]
    _require(spec["status"] == "local_only", "NFCorpus must be local_only")
    _require(spec["redistribution"] == "unresolved", "NFCorpus redistribution must be unresolved")
    _require(spec["source_md5"] == "a89dba18a62ef92f7d323ec890a0d38d", "NFCorpus source MD5 drift")
    _require(spec["source"].startswith("https://public.ukp.informatik.tu-darmstadt.de/"), "unexpected NFCorpus source")
    return spec


NF_CORPUS_ARCHIVE_MD5 = "a89dba18a62ef92f7d323ec890a0d38d"

# Official BEIR nfcorpus.zip member inventory (verified from the official
# archive): directory entries plus the payload files. Payloads read: corpus,
# queries, qrels/{test,train,dev}.tsv. The official archive also carries
# directory entries nfcorpus/ and nfcorpus/qrels/.
NF_CORPUS_MEMBERS = (
    "nfcorpus/",
    "nfcorpus/qrels/",
    "nfcorpus/corpus.jsonl",
    "nfcorpus/queries.jsonl",
    "nfcorpus/qrels/test.tsv",
    "nfcorpus/qrels/train.tsv",
    "nfcorpus/qrels/dev.tsv",
)
NF_CORPUS_READ_PAYLOADS = (
    "nfcorpus/corpus.jsonl",
    "nfcorpus/queries.jsonl",
    "nfcorpus/qrels/test.tsv",
)


def nfcorpus_parse(archive: Path, manifest: dict) -> dict:
    """Parse the LOCAL official NFCorpus archive (zip).

    `archive` must be the official BEIR nfcorpus.zip. The adapter streams the
    archive to compute its MD5, which MUST equal the frozen official archive
    MD5 (a89dba18…); there is no override. It then accepts ONLY the official
    member inventory (directory entries + the payload files above), reading
    corpus/queries/qrels/test.tsv. Member path traversal, duplicate members,
    unknown payload members, and missing members are all rejected; the official
    train/dev qrels are accepted as known members but not parsed here. A
    missing qrels/test.tsv is a hard error (never silently None). Raw rows are
    returned in memory only.

    Raises AdapterError on any violation; errors never leak local paths.
    """
    nfcorpus_contract(manifest)  # validate the frozen contract first
    if not archive.is_file():
        raise AdapterError("NFCorpus archive file is missing or unreadable")
    md5 = _stream_md5(archive)
    if md5 != NF_CORPUS_ARCHIVE_MD5:
        raise AdapterError("NFCorpus official archive MD5 does not match the frozen value")

    import zipfile

    try:
        with zipfile.ZipFile(archive) as zf:
            _validate_nfcorpus_members(zf)
            corpus = _read_json_array_bytes(zf.read("nfcorpus/corpus.jsonl"))
            queries = _read_json_array_bytes(zf.read("nfcorpus/queries.jsonl"))
            qrels = _read_qrels_bytes(zf.read("nfcorpus/qrels/test.tsv"))
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("NFCorpus archive could not be read safely") from exc

    for row in corpus:
        for field in ("_id", "text", "title"):
            if field not in row:
                raise AdapterError(f"NFCorpus corpus row missing field {field!r}")
    for row in queries:
        if "_id" not in row or "text" not in row:
            raise AdapterError("NFCorpus queries row missing _id/text")
    return {"corpus": corpus, "queries": queries, "qrels_test": qrels}


def _stream_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_nfcorpus_members(zf) -> None:
    """Validate the zip member inventory against the OFFICIAL nfcorpus.zip
    inventory: directory entries plus the fixed payload files. Rejects
    duplicates, unknown payload members, traversal, and missing required
    payloads. Accepts the official train/dev qrels as known members."""
    allowed = set(NF_CORPUS_MEMBERS)
    read_payloads = set(NF_CORPUS_READ_PAYLOADS)
    names = zf.namelist()
    seen = set()
    for name in names:
        if name in seen:
            raise AdapterError("NFCorpus archive contains duplicate members")
        seen.add(name)
    for name in names:
        if name not in allowed:
            raise AdapterError(f"NFCorpus archive contains unexpected member: {name}")
    if not read_payloads.issubset(seen):
        raise AdapterError("NFCorpus archive is missing a required member")


def miracl_contract(manifest: dict) -> dict:
    """Frozen MIRACL contract: adapter-only, not_run / not_comparable_to_official.
    No corpus download and no committed topics/qrels."""
    spec = manifest["adapters"]["miracl"]
    _require(spec["status"] == "adapter_only", "MIRACL must be adapter_only")
    _require(spec["redistribution"] == "not_run", "MIRACL redistribution must be not_run")
    _require(spec["revision"] == "5be20db9509754dadad47689368639fcec739c00", "MIRACL revision drift")
    for entry in spec["files"]:
        _require(isinstance(entry["bytes"], int) and entry["bytes"] > 0, f"bad MIRACL file bytes {entry}")
        _require(entry["lang"] in {"zh", "ja", "en"}, f"bad MIRACL lang {entry}")
        _require(entry["path"].startswith(("topics/", "qrels/")), f"bad MIRACL path {entry}")
    return spec


def validate_adapters(manifest_path: Path) -> dict:
    """Validate all adapter contracts against the committed manifest."""
    manifest = load_manifest(manifest_path)
    return {
        "longmemeval": longmemeval_contract(manifest),
        "nfcorpus": nfcorpus_contract(manifest),
        "miracl": miracl_contract(manifest),
    }
