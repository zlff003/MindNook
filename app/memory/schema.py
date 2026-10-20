"""Small, idempotent SQLite/MySQL migration following the project's existing style."""
from sqlalchemy import inspect, text


def migrate_memory_schema(engine) -> None:
    with engine.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        if "tool_jobs" in tables:
            columns = {item["name"] for item in inspector.get_columns("tool_jobs")}
            if "result_json" not in columns:
                connection.execute(text("ALTER TABLE tool_jobs ADD COLUMN result_json TEXT NULL"))
        if "chat_messages" in tables:
            indexes = {item["name"] for item in inspector.get_indexes("chat_messages")}
            if "ix_messages_memory_window" not in indexes:
                connection.execute(text(
                    "CREATE INDEX ix_messages_memory_window ON chat_messages (conversation_id, status, id)"
                ))
    # Minimal legacy-schema tests may have only tool_jobs/chat_messages.
    if {"users", "conversations", "user_memories", "conversation_summaries"} <= tables:
        _migrate_unverified_memories(engine)


def _migrate_unverified_memories(engine):
    """Retain old values in an audit ledger; rebuild projections from original messages."""
    import json
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.models import Conversation, ConversationSummary, MemoryBatch, MemoryEvent, UserMemory
    from app.memory.repository import dumps, enqueue_index, memory_dict, snapshot_upper
    from app.services.jobs import enqueue_tool_job
    MemoryBatch.__table__.create(engine, checkfirst=True)
    MemoryEvent.__table__.create(engine, checkfirst=True)
    with Session(engine) as db:
        affected = set()
        for row in db.scalars(select(UserMemory).where(UserMemory.status != "legacy_unverified")):
            evidence = json.loads(row.evidence_json)
            if evidence and all(e.get("quote") and e.get("db_id") for e in evidence):
                continue
            conversation = db.scalar(select(Conversation).where(Conversation.student_id == row.student_id)
                                     .order_by(Conversation.id).limit(1))
            if conversation is None:
                continue
            db.add(MemoryEvent(event_key=f"legacy-fact:{row.public_id}:{row.version}", student_id=row.student_id,
                conversation_id=conversation.id, source_message_id=evidence[0].get("message_id", "") if evidence else "",
                memory_id=row.public_id, relation="legacy_archive", quote=row.content, before_json=dumps(memory_dict(row))))
            row.status = "legacy_unverified"
            row.version += 1
            enqueue_index(db, row)
            affected.add(row.student_id)
        for state, conversation in db.execute(select(ConversationSummary, Conversation).join(Conversation,
                                              Conversation.id == ConversationSummary.conversation_id)):
            items = json.loads(state.summary_json)
            if conversation.student_id not in affected and not any(not i.get("memory_id") for i in items):
                continue
            db.add(MemoryEvent(event_key=f"legacy-summary:{conversation.id}:{state.version}",
                student_id=conversation.student_id, conversation_id=conversation.id, source_message_id="",
                relation="legacy_archive", quote="旧版摘要存档", before_json=dumps({
                    "summary": items, "summarized_until": state.summarized_until_message_id,
                    "extracted_until": state.extracted_until_message_id})))
            state.summary_json = "[]"
            state.summarized_until_message_id = state.extracted_until_message_id = 0
            state.version += 1
            # A mixed-version database can already contain successful v2 batches.
            # Keep their audit records, but free their work keys before rewinding cursors.
            for batch in db.scalars(select(MemoryBatch).where(MemoryBatch.conversation_id == conversation.id)):
                batch.batch_key = f"archived:{batch.id}:migration:{state.version}"
                batch.status = "archived"
            upper = snapshot_upper(db, conversation.id)
            enqueue_tool_job(db, job_type="MEMORY_EXTRACT",
                idempotency_key=f"memory-v2-rebuild:{conversation.id}:{state.version}",
                payload={"conversation_id": conversation.id, "through": upper}, priority=5)
        db.commit()
