from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_student
from app.db import get_db
from app.harness.agent_harness import ChatCommand
from app.models import Conversation, Message, User
from app.schemas import ChatRequest


router = APIRouter(prefix="/api/v1/conversations", tags=["student"])


def _conversation_for_student(
    db: Session,
    conversation_public_id: str,
    student: User,
) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.public_id == conversation_public_id,
            Conversation.student_id == student.id,
        )
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在。")
    return conversation


@router.post("")
def create_conversation(
    student: Annotated[User, Depends(require_student)],
    db: Annotated[Session, Depends(get_db)],
):
    conversation = Conversation(student_id=student.id)
    db.add(conversation)
    db.commit()
    return {
        "id": conversation.public_id,
        "title": conversation.title,
        "created_at": conversation.created_at,
    }


@router.get("")
def list_conversations(
    student: Annotated[User, Depends(require_student)],
    db: Annotated[Session, Depends(get_db)],
):
    conversations = list(
        db.scalars(
            select(Conversation)
            .where(Conversation.student_id == student.id)
            .order_by(Conversation.updated_at.desc())
        )
    )
    return [
        {
            "id": item.public_id,
            "title": item.title,
            "status": item.status,
            "updated_at": item.updated_at,
        }
        for item in conversations
    ]


@router.get("/{conversation_id}/messages")
def list_messages(
    conversation_id: str,
    student: Annotated[User, Depends(require_student)],
    db: Annotated[Session, Depends(get_db)],
):
    conversation = _conversation_for_student(db, conversation_id, student)
    messages = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.id.asc())
        )
    )
    return [
        {
            "id": item.public_id,
            "sender": item.sender_type,
            "content": item.content,
            "created_at": item.created_at,
        }
        for item in messages
    ]


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/{conversation_id}/messages:stream")
def stream_message(
    conversation_id: str,
    payload: ChatRequest,
    request: Request,
    student: Annotated[User, Depends(require_student)],
):
    command = ChatCommand(
        student_public_id=student.public_id,
        conversation_public_id=conversation_id,
        content=payload.content,
        client_request_id=payload.client_request_id,
    )

    def events() -> Iterator[str]:
        for event in request.app.state.chat_service.stream(command):
            yield _sse(event.type, event.payload)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
