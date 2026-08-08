from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.security import create_access_token, verify_password
from app.db import get_db
from app.models import User
from app.schemas import LoginRequest


router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.post("/auth/login")
def login(
    payload: LoginRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = db.scalar(select(User).where(User.username == payload.username))
    if not user or user.status != "active" or not verify_password(
        payload.password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误。",
        )
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
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
        },
    }


@router.get("/me")
def me(user: Annotated[User, Depends(get_current_user)]):
    return {
        "id": user.public_id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
    }

