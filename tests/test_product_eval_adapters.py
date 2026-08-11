"""Phase 4D adapter parser contract tests (task #18).

These are CI-safe (no network, no model, no credentials, no real dataset
download). They create tiny LOCAL fixtures in the OFFICIAL dataset shapes and
verify the runnable local-path parsers validate the frozen contract (pinned
sha256, byte size, official JSON-array fields, archive MD5 + safe zip member
reads, qrels presence) and fail closed on every violation.

The real manifest lists production byte sizes (up to ~2.7 GB); LongMemEval
tests substitute a SMALL stub manifest with tiny sizes but REAL pinned hashes
(sha256 of the stub content) so size/hash logic runs without huge files.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PRODUCT_EVAL = REPO / "benchmark" / "product_eval"
DATASET_MANIFEST = PRODUCT_EVAL / "dataset_manifest.json"


def _stub_manifest() -> dict:
    real = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
    stub = json.loads(json.dumps(real))
    for idx, f in enumerate(stub["adapters"]["longmemeval"]["files"]):
        f["bytes"] = 320000
        f["sha256"] = "0" * 63 + f"{idx}"
    return stub


def _official_instance(qid: str, *, abstain: bool = False) -> dict:
    suffix = "_abs" if abstain else ""
    evidence_turn = {"role": "assistant", "content": "hi", "has_answer": True}
    sessions = [
        [{"role": "user", "content": "hello"}, evidence_turn if not abstain else {"role": "assistant", "content": "hi"}],
        [{"role": "user", "content": "again"}, {"role": "assistant", "content": "ok"}],
    ]
    return {
        "question_id": f"{qid}{suffix}",
        "question_type": "knowledge-update",
        "question": f"question {qid}",
        "answer": f"answer {qid}",
        "question_date": "2026-08-01T00:00:00Z",
        "haystack_session_ids": ["s1", "s2"],
        "haystack_dates": ["2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"],
        "haystack_sessions": sessions,
        "answer_session_ids": ["s1"] if not abstain else [],
    }


def _official_instances(n: int = 500) -> list[dict]:
    """n unique official-shape instances (question_id q1..qn)."""
    return [_official_instance(f"q{i}") for i in range(1, n + 1)]


def _official_bytes(instances: list[dict], nbytes: int) -> bytes:
    text = json.dumps(instances)
    data = text.encode("utf-8")
    if len(data) > nbytes:
        data = data[:nbytes]
    else:
        data = data + b"\n" * (nbytes - len(data))
    return data


def _write_longmem(tmp_path: Path, manifest: dict, instances: list[dict]) -> None:
    for f in manifest["adapters"]["longmemeval"]["files"]:
        data = _official_bytes(instances, f["bytes"])
        f["sha256"] = hashlib.sha256(data).hexdigest()
        path = tmp_path / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _make_nfcorpus_zip(
    *,
    extra_member: str | None = None,
    missing: str | None = None,
    duplicate: bool = False,
) -> bytes:
    """Build a zip matching the OFFICIAL nfcorpus.zip member inventory."""
    corpus = '{"_id":"d1","text":"hello world","title":"t"}\n'
    queries = '{"_id":"q1","text":"greetings"}\n'
    test_qrels = "q1 d1 1\n"
    train_qrels = "q1 d1 2\n"
    dev_qrels = "q1 d1 3\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        members = {
            "nfcorpus/": "",
            "nfcorpus/qrels/": "",
            "nfcorpus/corpus.jsonl": corpus,
            "nfcorpus/queries.jsonl": queries,
            "nfcorpus/qrels/test.tsv": test_qrels,
            "nfcorpus/qrels/train.tsv": train_qrels,
            "nfcorpus/qrels/dev.tsv": dev_qrels,
        }
        if missing:
            members.pop(missing, None)
        if extra_member:
            members[extra_member] = "x"
        for name, content in members.items():
            zf.writestr(name, content)
        if duplicate:
            zf.writestr("nfcorpus/corpus.jsonl", corpus)
    return buf.getvalue()


def _write_nfcorpus_archive(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "nfcorpus.zip"
    path.write_bytes(data)
    return path


def test_longmemeval_parser_ok(tmp_path: Path):
    from benchmark.product_eval.adapters import longmemeval_parse

    manifest = _stub_manifest()
    _write_longmem(tmp_path, manifest, _official_instances())
    parsed = longmemeval_parse(tmp_path, manifest)
    assert set(parsed) == {f["path"] for f in manifest["adapters"]["longmemeval"]["files"]}


def test_longmemeval_parser_ok_with_one_abstention(tmp_path: Path):
    from benchmark.product_eval.adapters import longmemeval_parse

    manifest = _stub_manifest()
    insts = _official_instances()
    insts[3] = _official_instance("q_abs", abstain=True)
    insts[3]["question_id"] = "q4_abs"  # keep unique id
    _write_longmem(tmp_path, manifest, insts)
    parsed = longmemeval_parse(tmp_path, manifest)
    assert parsed


def test_longmemeval_parser_rejects_missing_file(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, longmemeval_parse

    with pytest.raises(AdapterError):
        longmemeval_parse(tmp_path, _stub_manifest())


def test_longmemeval_parser_rejects_sha256_mismatch(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, longmemeval_parse

    manifest = _stub_manifest()
    f = manifest["adapters"]["longmemeval"]["files"][0]
    path = tmp_path / f["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * f["bytes"])
    with pytest.raises(AdapterError):
        longmemeval_parse(tmp_path, manifest)


def test_longmemeval_parser_rejects_wrong_instance_count(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, longmemeval_parse

    manifest = _stub_manifest()
    _write_longmem(tmp_path, manifest, _official_instances(499))  # not 500
    with pytest.raises(AdapterError):
        longmemeval_parse(tmp_path, manifest)


def test_longmemeval_parser_rejects_non_abs_with_empty_evidence(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, longmemeval_parse

    manifest = _stub_manifest()
    insts = _official_instances()
    insts[0]["answer_session_ids"] = []
    _write_longmem(tmp_path, manifest, insts)
    with pytest.raises(AdapterError):
        longmemeval_parse(tmp_path, manifest)


def test_longmemeval_parser_rejects_abs_with_evidence_turn(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, longmemeval_parse

    manifest = _stub_manifest()
    insts = _official_instances()
    # Make q1 an abstention id but keep the evidence turn -> must fail.
    insts[0]["question_id"] = "q1_abs"
    insts[0]["answer_session_ids"] = []
    _write_longmem(tmp_path, manifest, insts)
    with pytest.raises(AdapterError):
        longmemeval_parse(tmp_path, manifest)


def test_longmemeval_parser_rejects_evidence_session_mismatch(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, longmemeval_parse

    manifest = _stub_manifest()
    insts = _official_instances()
    # evidence turn is in session s1 but answer_session_ids says s2.
    insts[0]["answer_session_ids"] = ["s2"]
    _write_longmem(tmp_path, manifest, insts)
    with pytest.raises(AdapterError):
        longmemeval_parse(tmp_path, manifest)


def test_longmemeval_parser_rejects_bad_session_ids_length(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, longmemeval_parse

    manifest = _stub_manifest()
    insts = _official_instances()
    insts[0]["haystack_dates"] = ["2026-07-01T00:00:00Z"]  # length mismatch (1 vs 2)
    _write_longmem(tmp_path, manifest, insts)
    with pytest.raises(AdapterError):
        longmemeval_parse(tmp_path, manifest)


def test_longmemeval_parser_rejects_bad_turn(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, longmemeval_parse

    manifest = _stub_manifest()
    insts = _official_instances()
    insts[0]["haystack_sessions"] = [[{"no": "role"}]]
    _write_longmem(tmp_path, manifest, insts)
    with pytest.raises(AdapterError):
        longmemeval_parse(tmp_path, manifest)


def test_nfcorpus_member_inventory_ok(tmp_path: Path):
    """The full official inventory (dirs + payloads + train/dev qrels) is
    accepted by the member validator."""
    from benchmark.product_eval.adapters import _validate_nfcorpus_members

    data = _make_nfcorpus_zip()
    archive = _write_nfcorpus_archive(tmp_path, data)
    with zipfile.ZipFile(archive) as zf:
        _validate_nfcorpus_members(zf)


def test_nfcorpus_parser_rejects_bad_md5(tmp_path: Path):
    """Public parser has NO md5 override: a synthetic archive (whose MD5 does
    not match the frozen official MD5) is always rejected."""
    from benchmark.product_eval.adapters import AdapterError, nfcorpus_parse

    manifest = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))  # frozen real MD5
    archive = _write_nfcorpus_archive(tmp_path, _make_nfcorpus_zip())
    with pytest.raises(AdapterError):
        nfcorpus_parse(archive, manifest)


def test_nfcorpus_parser_rejects_missing_qrels(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, _validate_nfcorpus_members

    data = _make_nfcorpus_zip(missing="nfcorpus/qrels/test.tsv")
    archive = _write_nfcorpus_archive(tmp_path, data)
    with zipfile.ZipFile(archive) as zf:
        with pytest.raises(AdapterError):
            _validate_nfcorpus_members(zf)


def test_nfcorpus_parser_rejects_extra_member(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, _validate_nfcorpus_members

    data = _make_nfcorpus_zip(extra_member="nfcorpus/evil.txt")
    archive = _write_nfcorpus_archive(tmp_path, data)
    with zipfile.ZipFile(archive) as zf:
        with pytest.raises(AdapterError):
            _validate_nfcorpus_members(zf)


def test_nfcorpus_parser_rejects_duplicate_member(tmp_path: Path):
    from benchmark.product_eval.adapters import AdapterError, _validate_nfcorpus_members

    data = _make_nfcorpus_zip(duplicate=True)
    archive = _write_nfcorpus_archive(tmp_path, data)
    with zipfile.ZipFile(archive) as zf:
        with pytest.raises(AdapterError):
            _validate_nfcorpus_members(zf)


def test_nfcorpus_parser_ok_real_archive(tmp_path: Path):
    """When the real official archive is provided via NF_CORPUS_ARCHIVE, the
    public parser must succeed (frozen MD5 + full official inventory)."""
    import os

    real = Path(os.environ.get("NF_CORPUS_ARCHIVE", ""))
    if not real.is_file():
        pytest.skip("real official nfcorpus.zip not provided via NF_CORPUS_ARCHIVE")
    from benchmark.product_eval.adapters import nfcorpus_parse

    manifest = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
    parsed = nfcorpus_parse(real, manifest)
    assert parsed["corpus"] and parsed["queries"] and parsed["qrels_test"]


def test_miracl_stays_contract_only():
    from benchmark.product_eval.adapters import miracl_contract

    spec = miracl_contract(json.loads(DATASET_MANIFEST.read_text(encoding="utf-8")))
    assert spec["status"] == "adapter_only"
    assert spec["redistribution"] == "not_run"
    assert spec["revision"] == "5be20db9509754dadad47689368639fcec739c00"


def test_adapters_no_network_imports():
    """adapters must not import network/model/credential deps."""
    import subprocess
    import sys

    code = (
        "import sys; "
        f"sys.path.insert(0, {str(REPO)!r}); "
        "from benchmark.product_eval import adapters; "
        "assert all(m not in sys.modules for m in ('requests','urllib.request','sentence_transformers','torch','transformers')); "
        "print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
