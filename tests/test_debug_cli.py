import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "mmag_debug_script",
    Path(__file__).resolve().parents[1] / "scripts" / "debug" / "__init__.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_terminal_reply = _MODULE._terminal_reply
_reply_exit_code = _MODULE._reply_exit_code


def test_terminal_reply_ignores_ack_and_stream_updates():
    common = {
        "user_id": "bot-1",
        "root_id": "post-1",
        "create_at": 200,
    }

    assert not _terminal_reply(
        {**common, "props": {"mmag_kind": "status", "mmag_status": "running"}},
        bot_uid="bot-1",
        root_post_id="post-1",
        created_at=100,
    )
    assert not _terminal_reply(
        {**common, "props": {"mmag_kind": "stream"}},
        bot_uid="bot-1",
        root_post_id="post-1",
        created_at=100,
    )


def test_terminal_reply_accepts_result_or_terminal_status_update():
    common = {
        "user_id": "bot-1",
        "root_id": "post-1",
        "create_at": 200,
    }

    assert _terminal_reply(
        {**common, "props": {"mmag_kind": "result"}},
        bot_uid="bot-1",
        root_post_id="post-1",
        created_at=100,
    )
    assert _terminal_reply(
        {**common, "props": {"mmag_kind": "status", "mmag_status": "failed"}},
        bot_uid="bot-1",
        root_post_id="post-1",
        created_at=100,
    )


def test_reply_exit_code_fails_for_error_and_timeout():
    assert _reply_exit_code(None) == 1
    assert _reply_exit_code({"props": {"mmag_kind": "error", "mmag_status": "failed"}}) == 1
    assert _reply_exit_code({"props": {"mmag_kind": "result", "mmag_status": "succeeded"}}) == 0
