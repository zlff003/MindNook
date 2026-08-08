from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=8, max_length=64)


class CaseNoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class CaseTransitionRequest(BaseModel):
    status: str = Field(pattern="^(pending|notified|assigned|following|closed)$")

