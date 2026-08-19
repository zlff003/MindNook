from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.db import get_db
from app.models import AuditLog, User
from app.schemas import AccountRegistrationRequest, LoginRequest


router = APIRouter(prefix="/api/v1", tags=["auth"])


def _auth_payload(user: User, request: Request) -> dict:
    settings = request.app.state.settings
    token = create_access_token(
        user.public_id,
        user.role,
        settings.app_secret,
        settings.access_token_ttl_seconds,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.public_id,
            "institutional_id": user.institutional_id,
            "name": user.name,
            "role": user.role,
        },
    }


@router.post("/auth/register/student", status_code=status.HTTP_201_CREATED)
def register_student(
    payload: AccountRegistrationRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    institutional_id = payload.institutional_id.strip()
    user = User(
        institutional_id=institutional_id,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
        role="student",
    )
    db.add(user)
    try:
        db.flush()
        db.add(
            AuditLog(
                actor_id=user.id,
                action="student_registered",
                resource_type="user",
                resource_public_id=user.public_id,
                detail=f"institutional_id={institutional_id}",
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该学号已注册。",
        ) from None
    return _auth_payload(user, request)


@router.post("/auth/login")
def login(
    payload: LoginRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = db.scalar(
        select(User).where(User.institutional_id == payload.institutional_id.strip())
    )
    if not user or user.status != "active" or not verify_password(
        payload.password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误。",
        )
    return _auth_payload(user, request)


@router.get("/me")
def me(user: Annotated[User, Depends(get_current_user)]):
    return {
        "id": user.public_id,
        "institutional_id": user.institutional_id,
        "name": user.name,
        "role": user.role,
    }
