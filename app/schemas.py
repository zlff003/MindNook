from pydantic import BaseModel, Field, model_validator


class LoginRequest(BaseModel):
    institutional_id: str = Field(
        min_length=1,
        max_length=128,
    )
    password: str = Field(min_length=1, max_length=256)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_username(cls, value):
        if isinstance(value, dict) and "institutional_id" not in value and "username" in value:
            value = {**value, "institutional_id": value["username"]}
        return value


class AccountRegistrationRequest(BaseModel):
    institutional_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    name: str = Field(min_length=1, max_length=128, pattern=r".*\S.*")
    password: str = Field(min_length=8, max_length=256)


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=8, max_length=64)


class CaseNoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class CaseTransitionRequest(BaseModel):
    status: str = Field(pattern="^(pending|notified|assigned|following|closed)$")


class CaseAssignmentRequest(BaseModel):
    owner_id: str = Field(min_length=36, max_length=36)
