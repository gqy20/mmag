import asyncio
import json
import logging

import pytest

from mmag.logger import ContextFilter, JSONFormatter, log_context, log_event


@pytest.mark.asyncio
async def test_log_context_is_isolated_and_resets_between_concurrent_tasks():
    ready = asyncio.Event()
    values: list[dict[str, str]] = []

    async def observe(value: str):
        with log_context.bind(trace_id=value, conversation_id=f"channel-{value}"):
            if value == "b":
                ready.set()
            await ready.wait()
            await asyncio.sleep(0)
            values.append(log_context.snapshot())

    await asyncio.gather(observe("a"), observe("b"))

    assert {item["trace_id"] for item in values} == {"a", "b"}
    assert log_context.snapshot() == {}


def test_json_logging_redacts_secrets_url_queries_and_exception_messages():
    record = logging.LogRecord(
        "mmag.test",
        logging.ERROR,
        __file__,
        1,
        "request authorization=Bearer abc url=%s error=%s",
        ("https://example.com/path?token=secret", RuntimeError("private body")),
        None,
    )
    record.event = "request.failed"
    ContextFilter().filter(record)

    payload = json.loads(JSONFormatter().format(record))

    assert payload["event"] == "request.failed"
    assert "abc" not in payload["message"]
    assert "secret" not in payload["message"]
    assert "private body" not in payload["message"]
    assert "RuntimeError" in payload["message"]


def test_log_event_adds_event_id_and_projects_run_correlation(caplog):
    logger = logging.getLogger("mmag.test.correlation")
    with (
        caplog.at_level(logging.INFO, logger=logger.name),
        log_context.bind(
            workflow_id="workflow-1",
            run_id="run-child",
            parent_run_id="run-parent",
            capability_call_id="call-1",
        ),
    ):
        log_event(logger, "agent.run.started", status="running")
        record = caplog.records[-1]
        ContextFilter().filter(record)
        payload = json.loads(JSONFormatter().format(record))

    assert len(payload["event_id"]) == 32
    assert payload["workflow_id"] == "workflow-1"
    assert payload["run_id"] == "run-child"
    assert payload["parent_run_id"] == "run-parent"
    assert payload["capability_call_id"] == "call-1"
