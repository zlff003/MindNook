from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import MemoryBatch, MemoryEvent, utc_now
from app.memory.repository import MemoryConflict


def claim_batch(db, settings, conversation_id, mode, first, last, *, background=False):
    key = f"v2:{conversation_id}:{mode}:{first}:{last}"
    values = dict(batch_key=key, conversation_id=conversation_id, mode=mode,
                  first_message_id=first, last_message_id=last, status="pending", attempts=0, error="")
    if db.get_bind().dialect.name == "mysql":
        stmt = mysql_insert(MemoryBatch).values(**values)
        db.execute(stmt.on_duplicate_key_update(batch_key=stmt.inserted.batch_key))
    else:
        db.execute(sqlite_insert(MemoryBatch).values(**values).on_conflict_do_nothing())
    row = db.scalar(select(MemoryBatch).where(MemoryBatch.batch_key == key))
    if row.status == "committed":
        db.commit()
        return None
    now = utc_now()
    eligible = or_(MemoryBatch.status.in_(("pending", "failed", "partial")),
                   (MemoryBatch.status == "running") & (MemoryBatch.retry_after <= now))
    stmt = update(MemoryBatch).where(MemoryBatch.id == row.id, eligible,
                                     MemoryBatch.attempts < settings.memory_batch_max_attempts)
    if not background:
        stmt = stmt.where(or_(MemoryBatch.retry_after.is_(None), MemoryBatch.retry_after <= now))
    count = db.execute(stmt.values(status="running", attempts=MemoryBatch.attempts + 1,
        retry_after=now + timedelta(seconds=settings.memory_model_timeout_seconds * 3 + 60), updated_at=now))
    if count.rowcount != 1:
        db.rollback()
        raise MemoryConflict("memory batch is running, deferred or exhausted; original messages remain available")
    db.refresh(row)
    token = (row.id, row.attempts)
    db.commit()
    return token


def finish_batch(db, token, *, error="", retry_seconds=60, partial=False):
    count = db.execute(update(MemoryBatch).where(MemoryBatch.id == token[0], MemoryBatch.attempts == token[1],
                        MemoryBatch.status == "running").values(status="partial" if partial else "failed" if error else "committed",
                        error=error[:1000], retry_after=utc_now() + timedelta(seconds=retry_seconds) if error else None,
                        updated_at=utc_now()))
    if count.rowcount != 1:
        raise MemoryConflict("memory batch lease was superseded")


def pending_correction(db, conversation, current):
    key = f"correction:{current.public_id}"
    row = db.scalar(select(MemoryEvent).where(MemoryEvent.event_key == key))
    if row is None:
        from app.services.privacy import redact_pii
        row = MemoryEvent(event_key=key, student_id=conversation.student_id, conversation_id=conversation.id,
                          source_message_id=current.public_id, relation="reconcile", status="pending",
                          quote=redact_pii(current.redacted_content or current.content))
        db.add(row)
        db.flush()
    return row
