"""Regression contract defined in docs/MEMORY_TEST_PLAN.md (M01–M21)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import func, inspect, select, text, update

from app.core.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.memory.index import MemoryIndex
from app.memory.repository import (
    ConversationBusy, MemoryConflict, acquire_turn, assert_turn, commit_update, dumps,
    ensure_summary, read_window, release_turn, snapshot_upper,
)
from app.memory.retrieval import recall
from app.memory.schema import migrate_memory_schema
from app.memory.service import MemoryService
from app.memory.store import InMemoryRedisStore
from app.memory.types import MemoryContext, MemoryMessage, MemoryUpdate
from app.models import Conversation, ConversationSummary, Message, ToolJob, User, UserMemory, utc_now
from app.services.context_budget import check_budget, estimate_tokens, input_limit, message_tokens
from app.services.jobs import claim_due_jobs, enqueue_tool_job, process_pending_jobs, recover_stale_jobs
from app.services.model import MockModelProvider, ModelCallError, ModelMessage, OpenAICompatibleModelProvider


class ScriptedProvider(MockModelProvider):
    def __init__(self, script=None):
        self.script = script
        self.calls = []

    def complete(self, messages, *, model, temperature, json_mode, purpose):
        self.calls.append((purpose, tuple(messages)))
        response = super().complete(messages, model=model, temperature=temperature, json_mode=json_mode, purpose=purpose)
        if self.script and purpose.startswith("memory_"):
            result = self.script(json.loads(messages[-1].content))
            return replace(response, content=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
        return response


class FakeIndex:
    signature = "test-index"

    def __init__(self):
        self.hits = []
        self.queries = []
        self.writes = []
        self.fail = False

    def search(self, student_id, query, count):
        self.queries.append((student_id, query))
        if self.fail:
            raise OSError("index unavailable")
        return self.hits[:count]

    def similarity(self, query, content):
        return 0.9 if any(word in query and word in content for word in ("睡眠", "考试", "待办", "压力")) else 0.1

    def upsert(self, **kwargs):
        if self.fail:
            raise OSError("index unavailable")
        self.writes.append(kwargs)

    def delete(self, memory_id):
        if self.fail:
            raise OSError("index unavailable")
        self.writes.append({"deleted": memory_id})


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, app_env="test", database_url=f"sqlite:///{(tmp_path / 'memory.db').as_posix()}",
                        model_provider="mock", model_name="test", rag_embedding_provider="hash",
                        rag_embedding_dimension=128, rag_reranker_enabled=False,
                        memory_history_tokens=239, memory_batch_tokens=5000,
                        memory_foreground_model_calls=True, memory_batch_max_messages=200,
                        memory_preference_tokens=0, memory_raw_recall_tokens=0,
                        memory_idle_seconds=300, tool_worker_retry_base_seconds=0.01)
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    db = factory()
    student = User(institutional_id="one", name="One", password_hash="unused", role="student")
    other = User(institutional_id="two", name="Two", password_hash="unused", role="student")
    db.add_all([student, other])
    db.flush()
    conversation = Conversation(student_id=student.id)
    second = Conversation(student_id=student.id)
    foreign = Conversation(student_id=other.id)
    db.add_all([conversation, second, foreign])
    db.commit()
    provider, index, cache = ScriptedProvider(), FakeIndex(), InMemoryRedisStore()
    service = MemoryService(settings, cache, provider, index=index)
    yield dict(settings=settings, engine=engine, factory=factory, db=db, student=student, other=other,
               conversation=conversation, second=second, foreign=foreign, provider=provider,
               index=index, cache=cache, service=service)
    db.close()
    engine.dispose()


def seed(env, count, *, conversation=None, texts=None, start=0, status="completed"):
    db = env["db"]
    conversation = conversation or env["conversation"]
    rows = []
    for i in range(count):
        content = texts[i] if texts else f"{start+i:04d}"
        row = Message(conversation_id=conversation.id, sender_type="student" if i % 2 == 0 else "assistant",
                      content=content, redacted_content=content, status=status)
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def summary(env):
    return env["db"].get(ConversationSummary, env["conversation"].id, populate_existing=True)


def add_memory(env, source, content="考试时睡眠不好，列待办有帮助", *, owner=None, status="active", indexed=True):
    row = UserMemory(student_id=owner or env["student"].id, content=content, type="support_feedback",
                     topics_json='["睡眠", "待办"]',
                     evidence_json=dumps([{"message_id": source.public_id, "conversation_id": source.conversation_id,
                                           "role": source.sender_type, "at": source.created_at.isoformat(), "stage": "feedback"}]),
                     status=status, version=1, indexed_version=1 if indexed else 0,
                     index_signature=env["index"].signature if indexed else "", dedup_key=source.public_id)
    env["db"].add(row)
    env["db"].commit()
    return row


def compact_calls(env):
    return [json.loads(messages[-1].content) for purpose, messages in env["provider"].calls if purpose == "memory_compact"]


def test_m01_short_history_grows_without_summary(env):
    rows = seed(env, 19)
    context = env["service"].load(env["db"], env["conversation"], current_input="当前")
    assert [m.db_id for m in context.model_history] == [r.id for r in rows]
    assert not context.history_compressed and not context.memory_brief
    assert not compact_calls(env)


def test_m02_incremental_boundaries_20_then_32(env):
    rows = seed(env, 20)
    context = env["service"].load(env["db"], env["conversation"])
    assert summary(env).summarized_until_message_id == rows[11].id
    assert [m.db_id for m in context.model_history] == [r.id for r in rows[12:]]
    extra = seed(env, 11, start=20)
    context = env["service"].load(env["db"], env["conversation"])
    assert len(context.model_history) == 19 and len(compact_calls(env)) == 1
    last = seed(env, 1, start=31)
    env["service"].load(env["db"], env["conversation"])
    assert summary(env).summarized_until_message_id == extra[3].id
    assert [m["message_id"] for m in compact_calls(env)[1]["messages"]] == [r.public_id for r in rows[12:] + extra[:4]]


def test_m03_cache_40_is_not_history_limit_and_stale_tail_is_repaired(env):
    env["settings"].memory_history_tokens = 10000
    rows = seed(env, 60)
    svc = env["service"]
    svc._replace_cache(svc.key(env["conversation"].public_id), [svc._from_model(row) for row in rows[-40:-1]])
    context = svc.load(env["db"], env["conversation"])
    assert len(context.model_history) == 60 and not context.cache_hit
    assert context.model_history[0].db_id == rows[0].id
    assert len(env["cache"].lrange(svc.key(env["conversation"].public_id), 0, -1)) == 40


def test_m03_current_generating_message_never_enters_history(env):
    rows = seed(env, 4)
    current = seed(env, 1, texts=["本轮唯一输入"], status="generating")[0]
    ctx = env["service"].load(env["db"], env["conversation"], current_input=current.content, current_message=current)
    from app.agents.runtime import RuntimeInput
    messages = RuntimeInput(current.content, ctx).history_prompt_messages + [ModelMessage("user", current.content)]
    assert sum(m.content == current.content for m in messages) == 1
    assert [m.db_id for m in ctx.model_history] == [r.id for r in rows]


def test_m04_thousands_of_messages_advance_without_skips(env):
    env["settings"].memory_batch_tokens = 12000
    rows = seed(env, 2200)
    previous = 0
    all_sources = []
    for _ in range(20):
        before = len(compact_calls(env))
        ctx = env["service"].load(env["db"], env["conversation"])
        assert len(compact_calls(env)) - before <= env["settings"].memory_foreground_max_batches
        current = summary(env).summarized_until_message_id
        assert current >= previous
        previous = current
        if not ctx.background_incomplete:
            break
    else:
        pytest.fail("backlog did not converge")
    for call in compact_calls(env):
        all_sources.extend(item["message_id"] for item in call["messages"])
    assert all_sources + [item.message_id for item in ctx.model_history] == [row.public_id for row in rows]
    assert len(all_sources) == len(set(all_sources))
    assert len(ctx.memory_brief) <= 400


@pytest.mark.parametrize("bad", ["not json", "invented_source", "long_summary", "timeout"])
def test_m05_invalid_generation_never_advances_boundary(env, bad):
    rows = seed(env, 20)
    def response(payload):
        if bad == "timeout":
            raise ModelCallError("TIMEOUT", "timeout")
        if bad == "not json":
            return bad
        return {"summary_items": [{"kind": "concern", "text": "a" * (401 if bad == "long_summary" else 2),
                "source_message_ids": ["invented" if bad == "invented_source" else rows[0].public_id]}], "memory_operations": []}
    env["provider"].script = response
    ctx = env["service"].load(env["db"], env["conversation"])
    assert ctx.background_incomplete and summary(env).summarized_until_message_id == 0
    assert sum(estimate_tokens(item.content) + 8 for item in ctx.model_history) <= ctx.diagnostics["history_budget"]
    calls = list(env["db"].scalars(select(ToolJob).where(ToolJob.job_type == "MEMORY_CALL")))
    assert len(calls) == 1 and json.loads(calls[0].result_json)["status"] == "failed"


def candidate(source, **overrides):
    data = dict(action="add", content=source.content, quote=source.content, subject="user", type="concern", topics=["考试", "睡眠"],
                source_message_ids=[source.public_id], stage="reported", change="new")
    data.update(overrides)
    data["quote"] = data["content"]
    return data


def test_m06_transaction_rolls_back_summary_and_memory_on_outbox_failure(env):
    rows = seed(env, 20)
    env["provider"].script = lambda p: {"facts": [{"message_id": rows[0].public_id, "quote": rows[0].content,
        "type": "concern", "subject": "user", "stage": "reported"}],
        "reviewed_message_ids": [m["message_id"] for m in p["messages"]]}
    with patch("app.memory.facts.enqueue_index", side_effect=RuntimeError("SQL failure")):
        ctx = env["service"].load(env["db"], env["conversation"])
    assert ctx.background_incomplete and summary(env).summarized_until_message_id == 0
    assert env["db"].scalar(select(func.count()).select_from(UserMemory)) == 0


def test_m07_repeat_extraction_is_idempotent_and_leaves_summary_boundary(env):
    rows = seed(env, 2, texts=["我考试前睡眠不好", "可以先试试列待办"])
    service = env["service"]
    service.extract_pending(env["db"], env["conversation"])
    service.extract_pending(env["db"], env["conversation"])
    assert env["db"].scalar(select(func.count()).select_from(UserMemory)) == 1
    assert summary(env).summarized_until_message_id == 0
    assert summary(env).extracted_until_message_id == rows[-1].id
    assert sum(purpose == "memory_extract" for purpose, _ in env["provider"].calls) == 1


def test_m07_stale_summary_cas_is_rejected(env):
    s = ensure_summary(env["db"], env["conversation"].id)
    env["db"].commit()
    commit_update(env["db"], env["conversation"], s.version, MemoryUpdate(), {}, boundary=0)
    env["db"].commit()
    with pytest.raises(MemoryConflict):
        commit_update(env["db"], env["conversation"], 0, MemoryUpdate(), {}, boundary=100)
    env["db"].rollback()
    assert summary(env).summarized_until_message_id == 0


@pytest.mark.parametrize("kind", ["foreign_source", "foreign_target", "assistant_fact"])
def test_m08_sources_and_update_targets_are_validated(env, kind):
    rows = seed(env, 20)
    foreign = seed(env, 1, conversation=env["foreign"])[0]
    operation = candidate(foreign if kind == "foreign_source" else rows[1] if kind == "assistant_fact" else rows[0])
    if kind == "foreign_target":
        other_memory = add_memory(env, foreign, owner=env["other"].id)
        operation.update(action="update", memory_id=other_memory.public_id, expected_version=1)
    env["provider"].script = lambda _: {"summary_items": [], "memory_operations": [operation]}
    ctx = env["service"].load(env["db"], env["conversation"])
    assert ctx.background_incomplete and summary(env).summarized_until_message_id == 0


def setup_correction(env):
    rows = seed(env, 2, texts=["考试睡眠不好，试过列待办，有帮助", "我们继续观察睡眠"])
    memory = add_memory(env, rows[0])
    s = ensure_summary(env["db"], env["conversation"].id)
    s.summary_json = dumps([{"kind": "feedback", "text": memory.content, "source_message_ids": [rows[0].public_id]}])
    s.summarized_until_message_id = rows[-1].id
    s.extracted_until_message_id = rows[-1].id
    env["db"].commit()
    return rows, memory


def test_m09_explicit_correction_updates_memory_without_advancing_boundary(env):
    rows, memory = setup_correction(env)
    env["settings"].memory_history_tokens = 3000
    current = seed(env, 1, texts=["之前说错了，列待办其实没试过"], status="generating")[0]
    ctx = env["service"].load(env["db"], env["conversation"], current_input=current.content, current_message=current)
    env["db"].refresh(memory)
    assert memory.content == current.content and memory.version == 2
    assert any(item.get("invalidated") for item in json.loads(memory.evidence_json))
    assert summary(env).summarized_until_message_id == rows[-1].id
    assert "有帮助" not in ctx.memory_brief


def test_m09_correction_in_new_session_invalidates_previous_summary(env):
    rows, memory = setup_correction(env)
    env["settings"].memory_history_tokens = 3000
    current = seed(env, 1, conversation=env["second"], texts=["之前说错了，列待办其实没试过"], status="generating")[0]
    env["service"].load(env["db"], env["second"], current_input=current.content, current_message=current)
    ctx = env["service"].load(env["db"], env["conversation"])
    assert "有帮助" not in ctx.memory_brief


def test_m10_effect_change_preserves_temporal_evidence(env):
    _, memory = setup_correction(env)
    env["settings"].memory_history_tokens = 3000
    current = seed(env, 1, texts=["列待办以前有效，现在没用了"], status="generating")[0]
    env["service"].load(env["db"], env["conversation"], current_input=current.content, current_message=current)
    env["db"].refresh(memory)
    assert memory.content == current.content
    assert len(json.loads(memory.evidence_json)) == 2
    assert not any(item.get("invalidated") for item in json.loads(memory.evidence_json))


def test_m11_failed_correction_preserves_old_fact_and_enqueues_retry(env):
    _, memory = setup_correction(env)
    env["provider"].script = lambda _: "invalid"
    current = seed(env, 1, texts=["之前说错了，列待办其实没试过"], status="generating")[0]
    ctx = env["service"].load(env["db"], env["conversation"], current_input=current.content, current_message=current)
    env["db"].refresh(memory)
    assert memory.status == "active" and memory.version == 1
    assert ctx.background_incomplete
    assert env["db"].scalar(select(ToolJob.id).where(ToolJob.job_type == "MEMORY_RECONCILE"))


def test_m11_ambiguous_feedback_does_not_update_multiple_records(env):
    _, first = setup_correction(env)
    extra = seed(env, 2, texts=["考试散步有效", "考试还有另一种办法"])
    second = add_memory(env, extra[0], "考试散步有帮助")
    second.topics_json = '["散步"]'
    env["db"].commit()
    current = seed(env, 1, texts=["那个办法没用"], status="generating")[0]
    ctx = env["service"].load(env["db"], env["conversation"], current_input=current.content, current_message=current)
    env["db"].refresh(first)
    env["db"].refresh(second)
    assert first.version == second.version == 1
    assert ctx.diagnostics["needs_clarification"]


def test_m12_short_session_switch_extracts_and_recalls_without_summary(env):
    seed(env, 2, texts=["考试让我睡眠不好，列待办有帮助", "了解，我们继续观察"])
    env["service"].load(env["db"], env["conversation"])
    ctx = env["service"].load(env["db"], env["second"], current_input="考试期间睡眠又不好了")
    assert ctx.recalled_memories and ctx.recalled_memories[0]["evidence"]
    assert summary(env).summarized_until_message_id == 0


def test_m12_m13_real_chroma_search_same_session_and_owner_filter(env):
    index = MemoryIndex(env["settings"])
    index.upsert(memory_id="a", student_id=1, content="考试压力影响睡眠，睡眠不足", version=1)
    index.upsert(memory_id="b", student_id=2, content="考试压力影响睡眠，睡眠不足", version=1)
    hits = index.search(1, "考试压力睡眠不足", 8)
    assert [item[0] for item in hits] == ["a"]
    index.upsert(memory_id="a", student_id=1, content="考试压力睡眠已改善", version=2)
    assert index.search(1, "考试压力睡眠", 8)[0][2] == 2
    index.delete("a")
    assert index.search(1, "睡眠", 8) == []


def test_m13_sql_rechecks_owner_even_if_index_returns_foreign_id(env):
    own = seed(env, 1)[0]
    foreign = seed(env, 1, conversation=env["foreign"])[0]
    add_memory(env, own)
    other = add_memory(env, foreign, owner=env["other"].id)
    env["index"].hits = [(other.public_id, 0.99, 1)]
    found, diagnostic = recall(env["db"], env["settings"], env["index"], env["student"].id, "考试睡眠", [], [])
    assert not found and diagnostic["discarded"][0]["reason"] == "inactive_or_wrong_owner"
    assert env["index"].queries[0][0] == env["student"].id


@pytest.mark.parametrize("case", ["unrelated", "covered_recent", "covered_summary", "partial_summary", "same_session"])
def test_m14_recall_filters_only_fully_covered_records(env, case):
    source = seed(env, 1)[0]
    memory = add_memory(env, source)
    env["index"].hits = [(memory.public_id, 0.1 if case == "unrelated" else 0.95, 1)]
    recent = [env["service"]._from_model(source)] if case == "covered_recent" else []
    items = [{"text": memory.content if case == "covered_summary" else "考试时睡眠不好",
              "source_message_ids": [source.public_id]}] if "summary" in case else []
    found, _ = recall(env["db"], env["settings"], env["index"], env["student"].id, "考试睡眠", recent, items)
    assert bool(found) == (case in ("partial_summary", "same_session"))


def test_m15_stale_vector_reads_current_body_and_retracted_is_filtered(env):
    source = seed(env, 1)[0]
    memory = add_memory(env, source)
    memory.content, memory.version = "考试睡眠改善，列待办现在没帮助", 2
    env["db"].commit()
    env["index"].hits = [(memory.public_id, 0.99, 1)]
    found, _ = recall(env["db"], env["settings"], env["index"], env["student"].id, "考试睡眠", [], [])
    assert found[0]["content"] == memory.content
    memory.status = "retracted"
    env["db"].commit()
    found, _ = recall(env["db"], env["settings"], env["index"], env["student"].id, "考试睡眠", [], [])
    assert not found


def test_m15_index_outage_does_not_rollback_memory_body(env):
    seed(env, 2, texts=["考试让我睡眠不好", "了解"])
    env["index"].fail = True
    env["service"].extract_pending(env["db"], env["conversation"])
    process_pending_jobs(env["factory"], env["settings"], env["service"])
    row = env["db"].scalar(select(UserMemory))
    assert row and row.indexed_version == 0
    job = env["db"].scalar(select(ToolJob).where(ToolJob.job_type == "MEMORY_INDEX_SYNC"))
    assert job.status == "PENDING" and job.attempt_count == 1
    env["index"].fail = False
    job.run_after = utc_now() - timedelta(seconds=1)
    env["db"].commit()
    process_pending_jobs(env["factory"], env["settings"], env["service"])
    env["db"].refresh(row)
    assert row.indexed_version == row.version


def test_m16_superseded_idle_job_does_no_model_work(env):
    rows = seed(env, 2)
    job = enqueue_tool_job(env["db"], job_type="MEMORY_EXTRACT", idempotency_key="idle",
                           payload={"conversation_id": env["conversation"].id, "through": rows[-1].id})
    env["db"].commit()
    seed(env, 2, start=2)
    result = env["service"].execute_job(env["db"], job)
    assert result["status"] == "superseded" and not env["provider"].calls


def test_m16_snapshot_does_not_cross_generating_gap(env):
    first = seed(env, 2)
    pending = seed(env, 1, status="generating")[0]
    seed(env, 2, start=3)
    assert snapshot_upper(env["db"], env["conversation"].id) == first[-1].id


def test_m17_lease_fences_out_expired_owner(env):
    db, conversation = env["db"], env["conversation"]
    token = acquire_turn(db, conversation.id, 60)
    with pytest.raises(ConversationBusy):
        acquire_turn(db, conversation.id, 60)
    db.execute(update(ConversationSummary).values(turn_expires_at=utc_now() - timedelta(seconds=1)))
    db.commit()
    new_token = acquire_turn(db, conversation.id, 60)
    with pytest.raises(ConversationBusy):
        assert_turn(db, conversation.id, token, 60)
    db.rollback()
    release_turn(db, conversation.id, new_token)
    assert summary(env).turn_token is None


def test_m07_m16_atomic_job_claim_and_live_worker_not_recovered(env):
    job = enqueue_tool_job(env["db"], job_type="MEMORY_EXTRACT", idempotency_key="claim", payload={})
    env["db"].commit()
    barrier = Barrier(2)
    def claim(worker):
        barrier.wait()
        return claim_due_jobs(env["factory"], worker_id=worker, batch_size=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["one", "two"]))
    assert sorted(map(len, results)) == [0, 1]
    assert recover_stale_jobs(env["factory"], 300) == 0
    env["db"].refresh(job)
    assert job.attempt_count == 1


def test_m18_per_model_hard_budget_rejects_before_provider(env):
    from app.agents.blackboard import CollaborationBlackboard
    env["settings"].model_context_limits = {"tiny": {"context_tokens": 1024, "output_tokens": 256}}
    board = CollaborationBlackboard(env["settings"], "mock")
    with pytest.raises(ModelCallError, match="budget"):
        board.call_model(env["provider"], agent="test", purpose="understanding", model="tiny",
                         temperature=0, json_mode=True, messages=[ModelMessage("user", "睡眠" * 500)])
    assert not env["provider"].calls


def test_m19_stream_usage_and_output_cap_are_preserved(env):
    from app.agents.blackboard import CollaborationBlackboard
    captured = {}
    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=(
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":7}}\n\n'
            'data: [DONE]\n\n').encode())
    settings = env["settings"].model_copy(update={"model_api_key": "fake", "model_base_url": "https://example.test"})
    provider = OpenAICompatibleModelProvider(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    board = CollaborationBlackboard(settings, provider.name)
    output = "".join(board.stream_model(provider, agent="test", purpose="response_generation", model="test",
                                      temperature=0, messages=[ModelMessage("user", "你好")]))
    assert output == "你好" and captured["max_tokens"] == settings.model_output_tokens
    step = board.trace.steps[-1]
    assert (step.input_tokens, step.output_tokens) == (123, 7) and "provider" in step.summary
    provider.close()


def test_m20_legacy_schema_migration_is_repeatable(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path/'legacy.db').as_posix()}")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE tool_jobs (id INTEGER PRIMARY KEY, payload_json TEXT)"))
        db.execute(text("INSERT INTO tool_jobs VALUES (1, '{}')"))
        db.execute(text("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, status TEXT)"))
    migrate_memory_schema(engine)
    migrate_memory_schema(engine)
    assert "result_json" in {column["name"] for column in inspect(engine).get_columns("tool_jobs")}
    assert "ix_messages_memory_window" in {index["name"] for index in inspect(engine).get_indexes("chat_messages")}
    with engine.connect() as db:
        assert db.scalar(text("SELECT payload_json FROM tool_jobs WHERE id=1")) == "{}"
    engine.dispose()


def test_m07_distinct_facts_in_one_source_are_not_silently_collapsed(env):
    source = seed(env, 1, texts=["考试影响睡眠；宿舍冲突影响学习"])[0]
    state = ensure_summary(env["db"], env["conversation"].id)
    env["db"].commit()
    result = MemoryUpdate.model_validate({"memory_operations": [
        candidate(source, content="考试影响睡眠"), candidate(source, content="宿舍冲突影响学习")
    ]})
    commit_update(env["db"], env["conversation"], state.version, result, {source.public_id: source}, boundary=source.id)
    env["db"].commit()
    assert env["db"].scalar(select(func.count()).select_from(UserMemory)) == 2
    state = summary(env)
    commit_update(env["db"], env["conversation"], state.version, result, {source.public_id: source}, boundary=source.id)
    env["db"].commit()
    assert env["db"].scalar(select(func.count()).select_from(UserMemory)) == 2


def test_m09_source_message_is_not_globally_retired(env):
    source = seed(env, 1, texts=["考试影响睡眠；宿舍冲突影响学习"])[0]
    state = ensure_summary(env["db"], env["conversation"].id)
    env["db"].commit()
    first = MemoryUpdate.model_validate({"memory_operations": [candidate(source, content="考试影响睡眠")]})
    commit_update(env["db"], env["conversation"], state.version, first, {source.public_id: source})
    env["db"].commit()
    memory = env["db"].scalar(select(UserMemory))
    memory.status = "retracted"
    env["db"].commit()
    second = MemoryUpdate.model_validate({"memory_operations": [candidate(source, content="宿舍冲突影响学习")]})
    commit_update(env["db"], env["conversation"], summary(env).version, second, {source.public_id: source})
    env["db"].commit()
    assert env["db"].scalar(select(func.count()).select_from(UserMemory)) == 2
    assert env["db"].scalar(select(UserMemory).where(UserMemory.content == "宿舍冲突影响学习")).status == "active"


def test_m04_topic_switch_archives_old_unresolved_episode(env):
    rows = seed(env, 20)
    rows[0].content = rows[0].redacted_content = "考试睡眠问题仍待观察"
    rows[2].content = rows[2].redacted_content = "当前讨论宿舍沟通"
    env["db"].commit()
    ctx = env["service"].load(env["db"], env["conversation"], current_input="现在想聊宿舍沟通")
    assert "宿舍沟通" in ctx.memory_brief
    memories = list(env["db"].scalars(select(UserMemory)))
    assert any("待观察" in memory.content and memory.status == "active" for memory in memories)


def test_m09_quarantined_summary_does_not_block_future_compaction(env):
    rows, memory = setup_correction(env)
    state = summary(env)
    items = json.loads(state.summary_json)
    items[0]["disputed"] = True
    state.summary_json = dumps(items)
    memory.status = "disputed"
    env["db"].commit()
    new_rows = seed(env, 20, start=2)
    ctx = env["service"].load(env["db"], env["conversation"])
    assert not ctx.background_incomplete
    assert summary(env).summarized_until_message_id == new_rows[11].id
    assert "有帮助" not in ctx.memory_brief
