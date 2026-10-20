"""Fault replays and worker integration from MEMORY_RELIABILITY_TEST_PLAN.md."""
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from tests.test_memory_upgrade import env, seed, summary, candidate, setup_correction
from app.memory.repository import commit_update, ensure_summary, dumps
from app.memory.types import MemoryUpdate
from app.memory.mock import mock_memory_update
from app.memory.history import raw_history, stable_preferences
from app.memory.schema import migrate_memory_schema
from app.models import MemoryBatch, MemoryEvent, Message, ToolJob, UserMemory, utc_now
from app.services.jobs import process_pending_jobs
from app.services.model import ModelCallError


def extract(env, messages=None):
    env["service"].extract_pending(env["db"], env["conversation"], background=True)
    return list(env["db"].scalars(select(UserMemory).order_by(UserMemory.id)))


def fact_payload(payload, source, quote):
    return {"facts": [{"message_id": source.public_id, "quote": quote, "subject": "user",
                       "type": "concern", "stage": "reported", "topics": []}],
            "reviewed_message_ids": [m["message_id"] for m in payload["messages"]]}


def test_r01_future_focus_cannot_be_bound_to_old_source(env):
    rows = seed(env, 20)
    rows[0].content = rows[0].redacted_content = "我担心考试，凌晨两点才睡。"
    env["db"].commit()
    seen = []
    def bad(p):
        seen.append(p)
        return fact_payload(p, rows[0], "我准备去问助教。")
    env["provider"].script = bad
    ctx = env["service"].load(env["db"], env["conversation"], current_input="我准备去问助教。")
    assert ctx.background_incomplete and summary(env).summarized_until_message_id == 0
    assert all("focus" not in p and "related_memories" not in p and "old_summary" not in p for p in seen)
    assert not env["db"].scalar(select(UserMemory.id))


@pytest.mark.parametrize("action", ["update", "retract"])
def test_r02_extractor_has_no_mutation_authority(env, action):
    rows, memory = setup_correction(env)
    result = MemoryUpdate.model_validate({"memory_operations": [candidate(rows[0], action=action,
        memory_id=memory.public_id, expected_version=1)]})
    with pytest.raises(ValueError, match="cannot mutate"):
        commit_update(env["db"], env["conversation"], summary(env).version, result, {rows[0].public_id: rows[0]})
    env["db"].rollback()
    assert memory.status == "active" and memory.version == 1


def test_r03_quote_cannot_drop_negation(env):
    source = seed(env, 1, texts=["我没试过清单"])[0]
    env["provider"].script = lambda p: fact_payload(p, source, "试过清单")
    with pytest.raises(ValueError, match="whole clauses"):
        extract(env)
    assert summary(env).extracted_until_message_id == 0


def test_r04_correct_one_fact_preserves_sibling_and_new_evidence(env):
    source = seed(env, 1, texts=["清单有帮助；散步是我做的"])[0]
    state = ensure_summary(env["db"], env["conversation"].id)
    env["db"].commit()
    initial = MemoryUpdate.model_validate({"memory_operations": [candidate(source, content="清单有帮助"),
                                                               candidate(source, content="散步是我做的")]})
    commit_update(env["db"], env["conversation"], state.version, initial, {source.public_id: source},
                  boundary=source.id, extracted_until=source.id)
    env["db"].commit()
    todo, walking = list(env["db"].scalars(select(UserMemory).order_by(UserMemory.id)))
    current = seed(env, 1, texts=["更正，清单是同学的；散步是我自己做的"])[0]
    correction = MemoryUpdate.model_validate({"memory_operations": [candidate(current, action="update",
        memory_id=todo.public_id, expected_version=1, target_quote=todo.content, change="correction")]})
    commit_update(env["db"], env["conversation"], summary(env).version, correction, {current.public_id: current},
                  mode="correct", verified_targets=[todo.public_id])
    env["db"].commit()
    env["db"].refresh(walking)
    assert walking.status == "active" and walking.version == 1
    assert any(item.get("memory_id") == walking.public_id for item in json.loads(summary(env).summary_json))
    new_fact = MemoryUpdate.model_validate({"memory_operations": [candidate(current, content="散步是我自己做的")]})
    commit_update(env["db"], env["conversation"], summary(env).version, new_fact, {current.public_id: current})
    env["db"].commit()
    assert env["db"].scalar(select(func.count()).select_from(UserMemory)) == 3
    event = env["db"].scalar(select(MemoryEvent).where(MemoryEvent.relation == "correction"))
    assert json.loads(event.before_json)["content"] == "清单有帮助"
    assert event.memory_id == todo.public_id
    # Same source/quote under a different type cannot resurrect the old fact.
    replay = MemoryUpdate.model_validate({"memory_operations": [candidate(source, content="清单有帮助", type="episode")]})
    commit_update(env["db"], env["conversation"], summary(env).version, replay, {source.public_id: source})
    env["db"].commit()
    assert env["db"].scalar(select(func.count()).select_from(UserMemory)) == 3


def test_r05_wrong_walking_retraction_is_vetoed(env):
    source = seed(env, 1, texts=["散步让我放松"])[0]
    walking = extract(env)[0]
    current = seed(env, 1, texts=["更正，清单是同学的，散步是我自己的经历"], status="generating")[0]
    def scripted(p):
        if p["mode"] == "correct":
            return {"corrections": [{"memory_id": walking.public_id, "expected_version": 1, "change": "correction"}]}
        if p["mode"] == "verify":
            return {"decisions": [{"memory_id": walking.public_id, "approved": False}]}
        return mock_memory_update(p)
    env["provider"].script = scripted
    env["settings"].memory_history_tokens = 3000
    env["service"].load(env["db"], env["conversation"], current_input=current.content, current_message=current)
    env["db"].refresh(walking)
    assert walking.status == "active" and walking.version == 1
    assert env["db"].scalar(select(MemoryEvent).where(MemoryEvent.relation == "reconcile")).status == "pending"
    assert not env["db"].scalar(select(MemoryEvent.id).where(MemoryEvent.relation == "correction"))


def test_r06_incomplete_review_rejects_entire_batch(env):
    seed(env, 2, texts=["我考试睡眠不好", "了解"])
    env["provider"].script = lambda p: {"facts": [], "reviewed_message_ids": [p["messages"][0]["message_id"]]}
    with pytest.raises(ValueError, match="fully reviewed"):
        extract(env)
    assert summary(env).extracted_until_message_id == 0
    batch = env["db"].scalar(select(MemoryBatch))
    assert batch.status == "failed"


def test_r06_truncated_output_records_usage_without_commit(env):
    seed(env, 2)
    original = env["provider"].complete
    with patch.object(env["provider"], "complete", side_effect=lambda *a, **k: replace(
        original(*a, **k), finish_reason="length", input_tokens=99, output_tokens=20)):
        with pytest.raises(ValueError, match="incomplete model output"):
            extract(env)
    job = env["db"].scalar(select(ToolJob).where(ToolJob.job_type == "MEMORY_CALL"))
    assert json.loads(job.result_json)["input_tokens"] == 99
    assert summary(env).extracted_until_message_id == 0


def test_r07_projection_omission_does_not_delete_durable_facts(env):
    env["settings"].memory_summary_max_chars = 150
    rows = seed(env, 20, texts=[("考试睡眠仍不好" * 10 + "。") if i % 2 == 0 else "了解" for i in range(20)])
    env["service"].load(env["db"], env["conversation"])
    stored = list(env["db"].scalars(select(UserMemory)))
    assert len(stored) >= 6 and len(json.loads(summary(env).summary_json)) < len(stored)
    assert all(r.status == "active" for r in stored)
    assert summary(env).summarized_until_message_id > 0


def test_r08_extract_cursor_does_not_skip_old_summary_range(env):
    rows = seed(env, 2, texts=["我考试凌晨两点才睡", "了解"])
    state = ensure_summary(env["db"], env["conversation"].id)
    state.summarized_until_message_id = rows[-1].id
    env["db"].commit()
    assert extract(env)[0].content == rows[0].content
    assert summary(env).extracted_until_message_id == rows[-1].id


def test_r09_foreground_failure_is_not_retried_every_turn(env):
    seed(env, 20)
    env["provider"].script = lambda p: "invalid"
    env["service"].load(env["db"], env["conversation"])
    calls = len(env["provider"].calls)
    env["service"].load(env["db"], env["conversation"])
    assert len(env["provider"].calls) == calls
    assert summary(env).summarized_until_message_id == 0
    env["provider"].script = None
    env["service"].load(env["db"], env["conversation"], background=True)
    assert summary(env).summarized_until_message_id > 0
    assert env["db"].scalar(select(MemoryBatch.id).where(MemoryBatch.status == "split"))


def test_r10_correction_survives_later_ordinary_message(env):
    _, memory = setup_correction(env)
    env["settings"].memory_foreground_model_calls = False
    current = seed(env, 1, texts=["之前说错了，列待办其实没试过"])[0]
    env["service"].load(env["db"], env["conversation"], current_input=current.content, current_message=current)
    seed(env, 2, texts=["现在聊别的", "好"])
    job = env["db"].scalar(select(ToolJob).where(ToolJob.job_type == "MEMORY_RECONCILE"))
    env["service"].execute_job(env["db"], job)
    env["db"].refresh(memory)
    assert memory.content == current.content


def test_r11_original_hour_recalled_without_any_extracted_memory(env):
    env["settings"].memory_raw_recall_tokens = 1600
    source = seed(env, 1, texts=["我担心考试，凌晨两点才睡"])[0]
    seed(env, 100, start=1)
    seed(env, 4, texts=["睡眠困扰和入睡时间最近都在变化，睡眠睡眠入睡入睡", "了解",
                        "现在睡眠问题涉及睡眠原因和入睡时间", "了解"])
    found = raw_history(env["db"], env["settings"], env["student"].id, "最初几点入睡，是什么原因", [])
    assert any("凌晨两点" in item["content"] for item in found)
    assert any(item["evidence"][0]["message_id"] == source.public_id for item in found)
    assert found[0]["evidence"][0]["message_id"] == source.public_id


def test_r11_raw_history_keeps_later_correction(env):
    env["settings"].memory_raw_recall_tokens = 1600
    seed(env, 2, texts=["列待办清单对我有帮助", "了解"])
    seed(env, 1, texts=["更正，清单有效是同学的经历，我没有试过"])
    found = raw_history(env["db"], env["settings"], env["student"].id, "最初清单有效吗", [])
    assert found
    assert all("同学" in item["content"] for item in found if "对我有帮助" in item["content"])


def test_r12_raw_history_never_reads_other_user(env):
    env["settings"].memory_raw_recall_tokens = 1600
    seed(env, 1, conversation=env["foreign"], texts=["紫色风筝让我睡不着"])
    assert not raw_history(env["db"], env["settings"], env["student"].id, "紫色风筝睡眠", [])


def test_r13_preference_has_independent_budget(env):
    seed(env, 1, texts=["希望你以后每次只问一个问题"])
    extract(env)
    env["settings"].memory_preference_tokens = 500
    assert stable_preferences(env["db"], env["settings"], env["student"].id, [])
    env["settings"].memory_preference_tokens = 10
    assert not stable_preferences(env["db"], env["settings"], env["student"].id, [])


def test_r14_legacy_migration_archives_and_is_repeatable(env):
    rows, memory = setup_correction(env)
    before = memory.content
    migrate_memory_schema(env["engine"])
    migrate_memory_schema(env["engine"])
    env["db"].expire_all()
    assert memory.content == before and memory.status == "legacy_unverified"
    assert summary(env).summarized_until_message_id == summary(env).extracted_until_message_id == 0
    assert env["db"].scalar(select(func.count()).select_from(Message)) == len(rows)
    assert env["db"].scalar(select(func.count()).select_from(MemoryEvent)) == 2


def test_r15_default_foreground_defers_and_worker_completes(env):
    from app.services.tool_dispatcher import InlineToolDispatcher
    env["settings"].memory_foreground_model_calls = False
    env["settings"].memory_idle_seconds = 0
    rows = seed(env, 2, texts=["我担心考试，凌晨两点才睡", "了解"])
    env["service"].load(env["db"], env["conversation"])
    assert not env["provider"].calls
    env["service"].enqueue_after_turn(env["db"], env["conversation"], *rows)
    env["db"].commit()
    dispatcher = InlineToolDispatcher(env["factory"], env["settings"])
    dispatcher.memory_service = env["service"]
    dispatcher.dispatch()
    env["db"].expire_all()
    stored = env["db"].scalar(select(UserMemory))
    assert stored and stored.indexed_version == stored.version
    assert summary(env).extracted_until_message_id == rows[-1].id
    assert env["db"].scalar(select(MemoryBatch.status)) == "committed"


def test_r16_outbox_failure_rolls_back_facts_and_events(env):
    seed(env, 2, texts=["考试影响睡眠", "了解"])
    with patch("app.memory.facts.enqueue_index", side_effect=RuntimeError("outbox unavailable")):
        with pytest.raises(RuntimeError, match="outbox"):
            extract(env)
    assert not env["db"].scalar(select(UserMemory.id))
    assert not env["db"].scalar(select(MemoryEvent.id))
    assert summary(env).extracted_until_message_id == 0


def test_r14_shadow_rebuild_preserves_original_and_snapshot(env, tmp_path):
    import sqlite3
    from scripts.memory_rebuild import prepare_shadow
    setup_correction(env)
    result = prepare_shadow(tmp_path / "memory.db", tmp_path / "shadow")
    assert result["activated"] is False
    for path in (tmp_path / "memory.db", result["snapshot"]):
        with sqlite3.connect(path) as connection:
            assert connection.execute("select count(*) from user_memories").fetchone()[0] == 1
            assert connection.execute("select count(*) from chat_messages").fetchone()[0] == 2
    with sqlite3.connect(result["shadow"]) as connection:
        assert connection.execute("select count(*) from user_memories").fetchone()[0] == 0
        assert connection.execute("select count(*) from chat_messages").fetchone()[0] == 2


def test_r15_threaded_dispatcher_persists_and_indexes(env):
    import time
    from app.services.tool_dispatcher import ThreadedToolDispatcher
    env["settings"].memory_idle_seconds = 0
    env["settings"].tool_worker_poll_seconds = 0.01
    rows = seed(env, 2, texts=["考试影响睡眠", "了解"])
    env["service"].enqueue_after_turn(env["db"], env["conversation"], *rows)
    env["db"].commit()
    worker = ThreadedToolDispatcher(env["factory"], env["settings"])
    worker.memory_service = env["service"]
    try:
        worker.start()
        worker.dispatch()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with env["factory"]() as check:
                row = check.scalar(select(UserMemory))
                if row is not None and row.indexed_version == row.version:
                    break
            time.sleep(0.02)
        else:
            pytest.fail("threaded memory worker did not persist and index within 8 seconds")
    finally:
        worker.close()


def test_independent_review_commits_only_approved_fact(env):
    seed(env, 1, texts=["列待办清单有帮助；散步让我放松"])
    todo, walking = extract(env)
    env["settings"].memory_history_tokens = 3000
    current = seed(env, 1, texts=["更正，清单是同学的，散步是我自己做的"], status="generating")[0]
    def scripted(payload):
        if payload["mode"] == "correct":
            return {"corrections": [{"memory_id": r.public_id, "expected_version": 1, "change": "correction"}
                                    for r in (todo, walking)]}
        if payload["mode"] == "verify":
            return {"decisions": [{"memory_id": todo.public_id, "approved": True},
                                  {"memory_id": walking.public_id, "approved": False}]}
        return mock_memory_update(payload)
    env["provider"].script = scripted
    env["service"].load(env["db"], env["conversation"], current_input=current.content, current_message=current)
    env["db"].refresh(todo)
    env["db"].refresh(walking)
    assert todo.version == 2 and todo.content == current.content
    assert walking.version == 1 and walking.status == "active"
    assert env["db"].scalar(select(MemoryBatch.status).where(MemoryBatch.mode == "correct")) == "partial"
    assert env["db"].scalar(select(MemoryEvent.status).where(MemoryEvent.relation == "reconcile")) == "pending"


def test_reported_alias_does_not_upgrade_plans_to_actions(env):
    from app.memory.types import ExtractedFact
    data = dict(message_id="source", quote="我计划试试", type="episode", subject="user", stage="stated")
    assert ExtractedFact.model_validate(data).stage == "reported"
    data["stage"] = "intended"
    assert ExtractedFact.model_validate(data).stage == "intended"


def test_quote_extraction_rejects_another_users_source(env):
    own = seed(env, 2)
    foreign = seed(env, 1, conversation=env["foreign"], texts=["考试让我睡不着"])[0]
    env["provider"].script = lambda p: fact_payload(p, foreign, foreign.content)
    with pytest.raises(ValueError, match="unauthorized"):
        extract(env)
    assert not env["db"].scalar(select(UserMemory.id))
    assert summary(env).extracted_until_message_id == 0


def test_stable_style_is_not_evicted_by_recent_temporary_preferences(env):
    from app.memory.types import is_stable_preference
    from app.memory.history import stable_preferences
    seed(env, 1, texts=["我一直希望你每次只问一个问题，回复尽量简短"])
    original = extract(env)[0]
    for i in range(25):
        row = UserMemory(student_id=env["student"].id, content=f"今天只想聊学习{i}", type="preference",
                         evidence_json=original.evidence_json, topics_json="[]", dedup_key=f"temporary-{i}")
        env["db"].add(row)
    env["db"].commit()
    env["settings"].memory_preference_tokens = 400
    found = stable_preferences(env["db"], env["settings"], env["student"].id, [])
    assert [r["memory_id"] for r in found] == [original.public_id]
    assert not is_stable_preference("这只是今天比较累，不是说我以后都拒绝建议")


def test_failed_turn_cannot_commit_a_correction(env):
    _, target = setup_correction(env)
    current = seed(env, 1, texts=["更正，列待办其实没试过"], status="failed")[0]
    result = MemoryUpdate.model_validate({"memory_operations": [candidate(current, action="update",
        memory_id=target.public_id, expected_version=1, target_quote=target.content, change="correction")]})
    with pytest.raises(ValueError, match="eligible student"):
        commit_update(env["db"], env["conversation"], summary(env).version, result, {current.public_id: current},
                      mode="correct", verified_targets=[target.public_id])
    env["db"].rollback()
    assert target.version == 1


def test_mixed_schema_rebuild_does_not_reuse_completed_batch_keys(env):
    rows = seed(env, 2, texts=["考试影响睡眠", "了解"])
    extract(env)
    env["db"].add(UserMemory(student_id=env["student"].id, content="旧版摘要事实", type="concern",
        evidence_json=dumps([{"message_id": rows[0].public_id}]), dedup_key="old-import"))
    env["db"].commit()
    migrate_memory_schema(env["engine"])
    env["db"].expire_all()
    assert summary(env).extracted_until_message_id == 0
    extract(env)
    assert summary(env).extracted_until_message_id == rows[-1].id
    states = list(env["db"].scalars(select(MemoryBatch.status)))
    assert "archived" in states and "committed" in states
