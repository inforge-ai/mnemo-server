"""Tests for structured JSON logging config."""
import io
import json
import logging
import sys

import pytest


def test_json_formatter_emits_required_fields():
    from mnemo.server.logging_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="my.module",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "my.module"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_passes_through_extra_fields():
    from mnemo.server.logging_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="my.module", level=logging.INFO, pathname=__file__,
        lineno=1, msg="event", args=(), exc_info=None,
    )
    record.event = "lifecycle_check"
    record.cosine = 0.73
    record.edge_created = True
    payload = json.loads(formatter.format(record))

    assert payload["event"] == "lifecycle_check"
    assert payload["cosine"] == 0.73
    assert payload["edge_created"] is True


def test_json_formatter_includes_exception_info():
    from mnemo.server.logging_config import JsonFormatter

    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="my.module", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))

    assert payload["level"] == "ERROR"
    assert "exception" in payload
    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_routes_records_through_json_formatter():
    from mnemo.server.logging_config import configure_logging, JsonFormatter

    buf = io.StringIO()
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        configure_logging(level="INFO", stream=buf)
        logging.getLogger("smoke").info("hi", extra={"event": "smoke", "k": 1})
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        line = buf.getvalue().strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["event"] == "smoke"
        assert payload["k"] == 1
        assert payload["message"] == "hi"
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_json_formatter_does_not_leak_internal_attrs():
    from mnemo.server.logging_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="my.module",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=99,
        msg="internal attrs should be hidden",
        args=(),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))

    internal_attrs = {"pathname", "lineno", "levelno", "thread", "msecs", "processName", "module"}
    leaked = internal_attrs & payload.keys()
    assert not leaked, f"Internal attrs leaked into JSON payload: {leaked}"


@pytest.mark.asyncio
async def test_lifespan_configures_json_logging(monkeypatch):
    """The FastAPI lifespan must call configure_logging()."""
    from mnemo.server import main as main_module
    from mnemo.server.logging_config import JsonFormatter

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = []
    root.setLevel(logging.WARNING)

    called = {"count": 0}
    real_configure = main_module.configure_logging

    def spy(*args, **kwargs):
        called["count"] += 1
        return real_configure(*args, **kwargs)

    monkeypatch.setattr(main_module, "configure_logging", spy)

    async def _noop_pool():
        class _Sentinel:
            async def close(self):
                return None
        return _Sentinel()

    async def _noop_close():
        return None

    monkeypatch.setattr(main_module, "create_pool", _noop_pool)
    monkeypatch.setattr(main_module, "close_pool", _noop_close)
    monkeypatch.setattr(main_module, "set_pool", lambda p: None)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr("mnemo.server.embeddings.warmup", lambda: None)
    monkeypatch.setattr("mnemo.server.services.migration_service.run_migrations", _noop)

    async def _consolidation_noop(pool):
        return None
    monkeypatch.setattr(
        "mnemo.server.services.consolidation.consolidation_loop", _consolidation_noop,
    )

    try:
        async with main_module.lifespan(main_module.app):
            assert called["count"] == 1
            assert any(isinstance(h.formatter, JsonFormatter) for h in logging.getLogger().handlers)
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
