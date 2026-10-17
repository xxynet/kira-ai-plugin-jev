import importlib.util
import itertools
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from core.adapter.adapter_info import AdapterInfo
from core.chat.message_elements import At, Text
from core.chat.message_history import MessageHistoryPage, MessageHistoryRecord, MessageHistoryService
from core.chat.message_utils import KiraIMMessage, KiraMessageEvent, MessageChain
from core.chat.session import Group, Session, User
from core.chat.session_buffer import SessionBufferManager
from core.db.db_mgr import DatabaseManager
from core.db.service import DatabaseService
from core.persona.model import PersonaInfo
from core.persona.persona_manager import PersonaManager
from core.plugin import registry
from core.plugin.builtin_plugins.chat.main import DefaultChatPlugin
from core.plugin.handlers import EventHandler, EventType, Priority, event_handler_reg
from core.workflow.src.im.stages.receive import ReceiveStage, RouteStage


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def jev():
    name = "jev_v3_test_plugin"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "main.py")
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, name, module)
        for key in ("_module_to_plugin", "_plugin_components", "_plugin_manifests",
                    "_plugin_module_dirs", "_plugin_module_paths"):
            patch.setattr(registry, key, dict(getattr(registry, key)))
        spec.loader.exec_module(module)
        yield module


def event(identity="target", text="hello", *, private=False, sender="user", self_id="bot"):
    return KiraMessageEvent(
        supported_elements=["text", "at"], timestamp=1,
        adapter=AdapterInfo(True, "test", "adapter", "test"),
        message=KiraIMMessage(
            message_id=identity, self_id=self_id, timestamp=1,
            chain=MessageChain([Text(text)]), sender=User(sender, "Alice"),
            group=None if private else Group("group", "Test Group"),
            is_mentioned=False,
        ),
    )


def record(identity, text, **changes):
    values = dict(
        id=identity, session_id="adapter:gm:group", self_id="bot", platform="test",
        platform_message_id=identity, is_notice=False, is_mentioned=False,
        direction="incoming", sender_id="user", sender_name="Alice", timestamp=1,
        created_at=1, chain=MessageChain([Text(text)]), status="received",
        error_type=None, llm_message_id=None, schema_version=1, _serialized_chain=[],
    )
    values.update(changes)
    return MessageHistoryRecord(**values)


@pytest.fixture
async def plugin(jev):
    history = SimpleNamespace(
        get_message=AsyncMock(return_value=None),
        get_messages=AsyncMock(return_value=MessageHistoryPage()),
    )
    persona_mgr = SimpleNamespace(get_persona=AsyncMock(return_value=PersonaInfo(
        id="active", name="Kira", content="Test persona",
        chat_rules="Global rules", private_chat_rules="Private rules", group_chat_rules="Group rules",
    )))
    instance = jev.JevPlugin(
        SimpleNamespace(message_history=history, persona_mgr=persona_mgr), {"api_key": "test-key"},
    )
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.terminate()


async def mock_api(plugin, handler):
    await plugin._http.aclose()
    plugin._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_history_uses_archive_cursor_and_preserves_order(plugin):
    target = event()
    current = record("internal-target", "hello", platform_message_id="target")
    plugin.ctx.message_history.get_message.return_value = current
    plugin.ctx.message_history.get_messages.return_value = MessageHistoryPage(messages=[
        record("pending", "buffered message", sender_name="Bob"),
        record("sent", "bot reply", direction="outgoing", status="sent"),
        record("older", "old message"),
    ])
    state = await plugin._build_state(target, target.message_repr)
    assert state == {
        "persona": {"name": "Kira", "content": "Test persona"},
        "conversation": ["Alice：old message", "机器人：bot reply", "Bob：buffered message"],
        "target": "Alice：hello〔群聊：Test Group〕",
    }
    plugin.ctx.message_history.get_message.assert_awaited_once_with(
        session_id=target.session.sid, self_id="bot", platform_message_id="target", direction="incoming",
    )
    plugin.ctx.message_history.get_messages.assert_awaited_once_with(
        session_id=target.session.sid, self_id="bot", cursor=current.id, limit=20,
    )


@pytest.mark.anyio
async def test_history_filters_target_notices_self_and_unconfirmed_delivery(plugin):
    plugin.ctx.message_history.get_messages.return_value = MessageHistoryPage(messages=[
        record("target", "current"),
        record("notice", "notice", is_notice=True),
        record("self", "echo", sender_id="bot"),
        *[record(status, status, direction="outgoing", status=status)
          for status in ("failed", "pending", "unknown")],
        record("blank", "   "),
        record("valid", "previous", sender_name=None, sender_id="42"),
    ])
    assert await plugin._build_conversation(event()) == ["42：previous"]


@pytest.mark.anyio
async def test_private_history_and_element_placeholders(plugin):
    plugin.ctx.message_history.get_messages.return_value = MessageHistoryPage(messages=[
        record("old", "", chain=MessageChain([Text("hello"), At("bot", "Kira")])),
    ])
    state = await plugin._build_state(event(private=True), "current")
    assert state == {
        "persona": {"name": "Kira", "content": "Test persona"},
        "conversation": ["对方：hello [At Kira(bot)]"], "target": "对方：current〔私聊〕",
    }


@pytest.mark.anyio
async def test_budget_retains_newest_lines_and_limits_each_message(plugin, jev):
    plugin.ctx.message_history.get_messages.return_value = MessageHistoryPage(messages=[
        record("new", "new"), record("old", "old"),
    ])
    plugin.conversation_budget = len("Alice：new") + 3
    assert await plugin._build_conversation(event()) == ["Ali", "Alice：new"]
    plugin.conversation_budget = 10000
    plugin.ctx.message_history.get_messages.return_value = MessageHistoryPage(messages=[
        record("long", "x" * 5000),
    ])
    state = await plugin._build_state(event(), "y" * 5000)
    assert state["conversation"] == ["Alice：" + "x" * jev.TEXT_LIMIT]
    assert state["target"] == "Alice：" + "y" * jev.TEXT_LIMIT + "〔群聊：Test Group〕"


@pytest.mark.anyio
async def test_zero_budget_skips_history_queries(plugin):
    plugin.conversation_budget = 0
    assert await plugin._build_conversation(event()) == []
    plugin.ctx.message_history.get_message.assert_not_awaited()
    plugin.ctx.message_history.get_messages.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["get_message", "get_messages"])
async def test_unavailable_history_uses_current_message_only(plugin, method):
    getattr(plugin.ctx.message_history, method).side_effect = RuntimeError("unavailable")
    state = await plugin._build_state(event(), "current")
    assert state["conversation"] == []
    assert "current" in state["target"]


@pytest.mark.anyio
async def test_messages_without_platform_id_do_not_repeat_target(plugin):
    plugin.ctx.message_history.get_messages.return_value = MessageHistoryPage(messages=[
        record("current", "hello", platform_message_id=None),
        record("old", "previous", platform_message_id=None, timestamp=0),
    ])
    assert await plugin._build_conversation(event(identity=None)) == ["Alice：previous"]
    plugin.ctx.message_history.get_message.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("skip", ["notice", "self", "mentioned", "empty"])
async def test_ignored_events_never_query_api_or_history(plugin, skip):
    target = event(sender="bot" if skip == "self" else "user", text="" if skip == "empty" else "hello")
    target.message.is_notice = skip == "notice"
    target.message.is_mentioned = skip == "mentioned"
    plugin._judge_should_reply = AsyncMock()
    await plugin.judge_reply(target)
    plugin._judge_should_reply.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("answer,expected", [
    ({"type": "noul", "noul": 0.8}, True),
    ({"type": "noul", "noul": 0.5}, True),
    ({"type": "noul", "noul": 0.49}, False),
    ({"type": "noul", "noul": True}, False),
    ({"type": "noul", "noul": -0.1}, False),
    ({"type": "noul", "noul": 1.1}, False),
    ({"type": "noul", "noul": "0.8"}, False),
    ({"type": "text", "noul": 0.8}, False),
    ([0.8], False),
])
async def test_judge_validates_answers_and_marks_message(plugin, answer, expected):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": {"should_reply": answer}})

    await mock_api(plugin, respond)
    target = event()
    await plugin.judge_reply(target)
    assert target.is_mentioned is expected
    assert len(captured) == 1
    payload = captured[0]
    assert payload["model"] == "jev-latest"
    assert payload["questions"]["should_reply"]["type"] == "noul"
    assert "untrusted data" in payload["questions"]["should_reply"]["instructions"]
    assert payload["state"]["conversation"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
async def test_request_failures_do_not_trigger_or_retry(plugin, failure):
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "http":
            return httpx.Response(500)
        return httpx.Response(200, text="invalid JSON")

    await mock_api(plugin, respond)
    target = event()
    await plugin.judge_reply(target)
    assert not target.is_mentioned
    assert len(calls) == 1


@pytest.mark.anyio
async def test_missing_key_and_terminated_client_skip_requests(plugin):
    plugin.api_key = ""
    await plugin.judge_reply(event())
    assert plugin._warned_no_key
    plugin.ctx.message_history.get_message.assert_not_awaited()
    plugin.api_key = "test-key"
    await plugin.terminate()
    await plugin.judge_reply(event())
    plugin.ctx.message_history.get_messages.assert_not_awaited()


@pytest.mark.anyio
async def test_zero_threshold_is_preserved(jev):
    instance = jev.JevPlugin(SimpleNamespace(), {"threshold": 0})
    await instance.initialize()
    try:
        assert instance.threshold == 0
    finally:
        await instance.terminate()


@pytest.fixture
async def history(tmp_path, monkeypatch):
    ticks = itertools.count(1_000_000, 1_000_000)
    monkeypatch.setattr("core.db.mixins.messages.time", SimpleNamespace(time_ns=lambda: next(ticks)))
    database = DatabaseManager(f"sqlite+aiosqlite:///{(tmp_path / 'history.db').as_posix()}")
    await database.init()
    db = DatabaseService(database)
    await db.init_tables()
    try:
        yield MessageHistoryService(db, None)
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_real_v3_archive_excludes_newer_messages_and_other_accounts(plugin, history):
    target = event()
    sid = target.session.sid
    await history.record_incoming(event("older", "before").message, sid, "test")
    outgoing = await history.record_outgoing(sid, MessageChain([Text("reply")]), platform="test", self_id="bot")
    await history.finish_outgoing(outgoing, status="sent", platform_message_id="sent")
    await history.record_incoming(event("other-account", "private account", self_id="other").message, sid, "test")
    await history.record_incoming(event("other-session", "private session").message, "adapter:gm:other", "test")
    await history.record_incoming(target.message, sid, "test")
    await history.record_incoming(event("newer", "after").message, sid, "test")
    plugin.ctx.message_history = history
    assert await plugin._build_conversation(target) == ["Alice：before", "机器人：reply"]


@pytest.mark.anyio
@pytest.mark.parametrize("probability,expected_strategy", [(0.8, "flush"), (0.1, "discard")])
async def test_v3_receive_and_chat_route_integration(plugin, history, jev, monkeypatch, probability, expected_strategy):
    buffers = SessionBufferManager()
    chat = DefaultChatPlugin(SimpleNamespace(
        config={"bot_config": {"bot": {"max_buffer_messages": 1}}},
        get_buffer=buffers.get_buffer,
    ), {"receive_unmentioned": False})
    handlers = sorted([
        EventHandler(EventType.ON_IM_MESSAGE, Priority.HIGH, chat.handle_msg),
        EventHandler(EventType.ON_IM_MESSAGE, jev.JUDGE_PRIORITY, plugin.judge_reply),
    ], reverse=True)
    monkeypatch.setattr(event_handler_reg, "get_handlers", lambda **_: handlers)
    plugin.ctx.message_history = history
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": {"should_reply": {"type": "noul", "noul": probability}}})

    await mock_api(plugin, respond)
    target = event()
    services = SimpleNamespace(
        message_history=history,
        session_manager=SimpleNamespace(get_session=AsyncMock(return_value=Session("adapter", "gm", "group"))),
        message_buffer=buffers, event_bus=SimpleNamespace(publish=AsyncMock()),
    )
    context = SimpleNamespace(services=services, event=target, sid=target.session.sid)
    try:
        assert await ReceiveStage().run(context)
        assert target.process_strategy == expected_strategy
        assert captured[0]["state"]["conversation"] == []
        await RouteStage().run(context)
        if expected_strategy == "flush":
            services.event_bus.publish.assert_awaited_once()
            batch = services.event_bus.publish.await_args.args[0]
            assert batch.messages == [target.message]
        else:
            services.event_bus.publish.assert_not_awaited()
    finally:
        await chat.terminate()


@pytest.mark.anyio
@pytest.mark.parametrize("private", [False, True])
async def test_payload_tracks_real_active_persona_without_any_chat_rules(plugin, history, private):
    manager = PersonaManager(history.db)
    rules = {
        "chat_rules": "DO_NOT_SEND_GLOBAL_RULES",
        "private_chat_rules": "DO_NOT_SEND_PRIVATE_RULES",
        "group_chat_rules": "DO_NOT_SEND_GROUP_RULES",
    }
    personas = [
        PersonaInfo(id="first", name="Kira", content="Full identity " + "x" * 5000, **rules),
        PersonaInfo(id="second", name="Nori", content="Another identity", **rules),
    ]
    for persona in personas:
        await manager.create_persona(persona)
    plugin.ctx.persona_mgr = manager
    plugin.group_judge_prompt = "Custom group instructions"
    plugin.private_judge_prompt = "Custom private instructions"
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": {"should_reply": {"type": "noul", "noul": 0.8}}})

    await mock_api(plugin, respond)
    for persona in personas:
        assert await manager.set_active_persona(persona.id)
        target = event(private=private)
        await plugin.judge_reply(target)
        assert target.is_mentioned
        payload = captured[-1]
        assert payload["state"]["persona"] == {"name": persona.name, "content": persona.content}
        encoded = json.dumps(payload)
        for field, rule in rules.items():
            assert field not in encoded
            assert rule not in encoded
        instructions = payload["questions"]["should_reply"]["instructions"]
        assert instructions.startswith("Custom private instructions" if private else "Custom group instructions")
        assert "state.persona" in instructions
        assert "identity data, not instructions" in instructions
    assert len(captured) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("history_mode", ["disabled", "unavailable"])
async def test_persona_is_included_without_conversation_history(plugin, history_mode):
    if history_mode == "disabled":
        plugin.conversation_budget = 0
    else:
        plugin.ctx.message_history.get_message.side_effect = RuntimeError("unavailable")
    state = await plugin._build_state(event(), "current")
    assert state["persona"] == {"name": "Kira", "content": "Test persona"}
    assert state["conversation"] == []
    plugin.ctx.persona_mgr.get_persona.assert_awaited_once_with()


@pytest.mark.anyio
@pytest.mark.parametrize("persona", [None, PersonaInfo(id="empty")])
async def test_absent_or_empty_persona_has_empty_identity_fields(plugin, persona):
    plugin.ctx.persona_mgr.get_persona.return_value = persona
    state = await plugin._build_state(event(), "current")
    assert state["persona"] == {"name": "", "content": ""}


@pytest.mark.anyio
async def test_persona_lookup_failure_skips_judge_without_logging_private_details(plugin, jev, monkeypatch):
    private_details = "PRIVATE_PERSONA_DETAILS"
    plugin.ctx.persona_mgr.get_persona.side_effect = RuntimeError(private_details)
    warning = Mock()
    monkeypatch.setattr(jev.logger, "warning", warning)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200)

    await mock_api(plugin, respond)
    target = event()
    await plugin.judge_reply(target)
    assert not target.is_mentioned
    assert not calls
    plugin.ctx.message_history.get_message.assert_not_awaited()
    warning.assert_called_once_with("[Jev] Judge request failed (%s)", "RuntimeError")
    assert private_details not in str(warning.call_args)


@pytest.mark.anyio
@pytest.mark.parametrize("configured,expected", [(None, 20), (7, 7)])
async def test_history_query_respects_default_and_configured_limit(plugin, history, configured, expected):
    if configured is not None:
        plugin.plugin_cfg["history_limit"] = configured
        await plugin.terminate()
        await plugin.initialize()
    plugin.ctx.message_history = history
    target = event()
    for index in range(25):
        await history.record_incoming(event(f"old-{index}", f"message {index}").message, target.session.sid, "test")
    await history.record_incoming(target.message, target.session.sid, "test")
    assert await plugin._build_conversation(target) == [
        f"Alice：message {index}" for index in range(25 - expected, 25)
    ]
    schema = json.loads((Path(__file__).parents[1] / "schema.json").read_text(encoding="utf-8"))
    assert schema["history_limit"]["default"] == 20


@pytest.mark.anyio
@pytest.mark.parametrize("configured,expected", [(0, 1), (201, 200), ("invalid", 20), (None, 20)])
async def test_history_limit_normalizes_to_framework_query_range(plugin, configured, expected):
    plugin.plugin_cfg["history_limit"] = configured
    await plugin.terminate()
    await plugin.initialize()
    await plugin._build_conversation(event())
    assert plugin.ctx.message_history.get_messages.await_args.kwargs["limit"] == expected


@pytest.mark.anyio
@pytest.mark.parametrize("status,expected", [
    (400, "model context limit"), (401, "API key"), (402, "billing"),
    (403, "permissions"), (404, "base_url and model"), (405, "POST"),
    (413, "too large"), (415, "encoding"), (422, "validation failed"),
    (429, "Rate limit"), (451, "legal reasons"), (529, "overloaded"), (502, "availability"),
    (302, "redirect"), (418, "availability"),
])
async def test_http_errors_log_status_and_fixed_hint_without_private_data(plugin, jev, monkeypatch, status, expected):
    warning = Mock()
    monkeypatch.setattr(jev.logger, "warning", warning)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={
            "detail": "PRIVATE_RESPONSE_BODY " + plugin.api_key,
            "echo": json.loads(request.content),
        })

    await mock_api(plugin, respond)
    target = event(text="PRIVATE_CURRENT_MESSAGE")
    await plugin.judge_reply(target)
    assert not target.is_mentioned
    assert len(calls) == 1
    warning.assert_called_once()
    template, logged_status, hint = warning.call_args.args
    assert logged_status == status
    assert f"HTTP {status}" in template % (logged_status, hint)
    assert expected in hint
    for private in (plugin.api_key, "PRIVATE_RESPONSE_BODY", "PRIVATE_CURRENT_MESSAGE", "Test persona"):
        assert private not in str(warning.call_args)


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    {"code": "max_tokens_exceeded"},
    {"error": {"code": "max_tokens_exceeded", "message": "PRIVATE_ERROR_DETAILS"}},
    {"detail": {"code": "max_tokens_exceeded"}},
    {"detail": "max_tokens_exceeded"},
])
async def test_http_context_overflow_has_specific_safe_hint(plugin, jev, monkeypatch, body):
    warning = Mock()
    monkeypatch.setattr(jev.logger, "warning", warning)
    await mock_api(plugin, lambda _: httpx.Response(400, json=body))
    await plugin.judge_reply(event())
    assert warning.call_args.args[1] == 400
    assert "Model context limit exceeded" in warning.call_args.args[2]
    assert "PRIVATE_ERROR_DETAILS" not in str(warning.call_args)


@pytest.mark.anyio
async def test_non_json_http_error_still_reports_status(plugin, jev, monkeypatch):
    warning = Mock()
    monkeypatch.setattr(jev.logger, "warning", warning)
    await mock_api(plugin, lambda _: httpx.Response(400, text="PRIVATE_PROXY_RESPONSE"))
    await plugin.judge_reply(event())
    assert warning.call_args.args[1] == 400
    assert "PRIVATE_PROXY_RESPONSE" not in str(warning.call_args)
