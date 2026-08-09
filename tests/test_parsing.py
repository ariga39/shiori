import ingest


def _msg(role, content):
    return {"type": "message", "message": {"role": role, "content": content}}


# ── extract_text_from_message: role filtering ────────────────────────────────


def test_user_role_extracted_with_prefix():
    out = ingest.extract_text_from_message(_msg("user", "hello world here"))
    assert out == "[user] hello world here"


def test_assistant_role_extracted_with_prefix():
    out = ingest.extract_text_from_message(_msg("assistant", "a helpful reply"))
    assert out == "[assistant] a helpful reply"


def test_tool_role_skipped():
    assert ingest.extract_text_from_message(_msg("tool", "some tool output")) is None


def test_toolResult_role_skipped():
    assert ingest.extract_text_from_message(_msg("toolResult", "result here")) is None


def test_system_role_skipped():
    assert ingest.extract_text_from_message(_msg("system", "system prompt")) is None


def test_image_role_skipped():
    assert ingest.extract_text_from_message(_msg("image", "some image path")) is None


def test_missing_role_skipped():
    out = ingest.extract_text_from_message({"type": "message", "message": {"content": "no role"}})
    assert out is None


# ── extract_text_from_message: content handling ──────────────────────────────


def test_content_list_joins_only_text_parts():
    obj = {
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "first part"},
                {"type": "image", "image": "ignored.png"},
                {"type": "toolCall", "id": "tc1"},
                "plain string part",
            ],
        }
    }
    out = ingest.extract_text_from_message(obj)
    assert out == "[user] first part\nplain string part"


def test_empty_content_returns_none():
    assert ingest.extract_text_from_message(_msg("user", "")) is None


def test_content_under_five_chars_returns_none():
    assert ingest.extract_text_from_message(_msg("user", "abc")) is None


def test_whitespace_only_content_returns_none():
    assert ingest.extract_text_from_message(_msg("user", "   ")) is None


def test_assistant_pure_tool_call_json_skipped():
    obj = {"message": {"role": "assistant", "content": '{"tool_calls": [{"id": "x"}]}'}}
    assert ingest.extract_text_from_message(obj) is None


# ── classify_session ─────────────────────────────────────────────────────────


def test_classify_subagent():
    lines = ["2026-08-03 00:00:00 - Subagent started"]
    assert ingest.classify_session("sess.jsonl", lines) == "subagent"


def test_classify_subagent_task_token():
    lines = ["SubagentTask(id=123)"]
    assert ingest.classify_session("sess.jsonl", lines) == "subagent"


def test_classify_cron():
    lines = ["[cron:0 9 * * *] running job"]
    assert ingest.classify_session("sess.jsonl", lines) == "cron"


def test_classify_cron_job_phrase():
    lines = ["this is a cron job run"]
    assert ingest.classify_session("sess.jsonl", lines) == "cron"


def test_classify_default_main_user():
    lines = ["a normal user session line"]
    assert ingest.classify_session("sess.jsonl", lines) == "main_user"
