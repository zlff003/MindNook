from __future__ import annotations

import json
import time
from dataclasses import dataclass
from collections.abc import Iterator
from typing import Protocol

import httpx

from app.core.config import Settings


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ModelResponse:
    content: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str | None = None


class ModelCallError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ModelProvider(Protocol):
    name: str

    def complete(
        self,
        messages: list[ModelMessage],
        *,
        model: str,
        temperature: float,
        json_mode: bool,
        purpose: str,
    ) -> ModelResponse:
        ...

    def stream(
        self,
        messages: list[ModelMessage],
        *,
        model: str,
        temperature: float,
        purpose: str,
    ) -> Iterator[str]:
        ...


class MockModelProvider:
    """Deterministic provider used by tests and offline development."""

    name = "mock"

    @staticmethod
    def _student_message(messages: list[ModelMessage]) -> str:
        for message in reversed(messages):
            if message.role == "user":
                return message.content
        return ""

    def complete(
        self,
        messages: list[ModelMessage],
        *,
        model: str,
        temperature: float,
        json_mode: bool,
        purpose: str,
    ) -> ModelResponse:
        del temperature, json_mode
        from app.safety import assess_message, high_risk_fallback

        started = time.perf_counter()
        student_message = self._student_message(messages)
        assessment = assess_message(student_message)

        if purpose == "understanding":
            content = json.dumps(
                {
                    "dialogue_type": assessment.dialogue_type,
                    "topics": ["general"]
                    if assessment.dialogue_type == "general"
                    else ["emotional_support"],
                    "needs_knowledge": assessment.dialogue_type != "general",
                    "confidence": assessment.confidence,
                    "reason": assessment.reason_summary,
                },
                ensure_ascii=False,
            )
        elif purpose == "risk_assessment":
            content = json.dumps(
                {
                    "risk_level": assessment.risk_level or "low",
                    "confidence": assessment.confidence,
                    "emotion_summary": assessment.emotion_summary
                    or "未识别到明显情绪困扰。",
                    "reason": assessment.reason_summary,
                    "immediate_danger": assessment.risk_level == "high",
                    "needs_safety_question": assessment.risk_level == "high",
                },
                ensure_ascii=False,
            )
        elif purpose == "response_generation":
            lowered = student_message.lower()
            if assessment.dialogue_type == "safety_risk":
                content = high_risk_fallback()
            elif assessment.dialogue_type == "psychological_support":
                content = (
                    "听起来这段困扰让你承受了不少压力，愿意说出来已经很不容易。"
                    "现在可以先把最急的一件事缩小成十分钟能完成的一步，再联系一位信任的人，"
                    "让对方知道你最近的状态。若困扰持续或影响生活，建议联系学校正式支持渠道。"
                )
            elif any(word in lowered for word in ("python", "编程", "代码")):
                content = (
                    "当然可以。先把问题拆成输入、处理和输出三部分，再写一个最小示例运行；"
                    "如果你把具体代码和报错贴出来，我可以继续帮你定位。"
                )
            else:
                content = (
                    f"关于“{student_message[:40]}”，你可以先说明最想解决的具体部分，"
                    "我会和你一起逐步梳理。"
                )
        elif purpose == "response_review":
            candidate = student_message
            forbidden = any(
                marker in candidate
                for marker in ("你的风险等级", "风险评分", "模型置信度", "诊断为")
            )
            content = json.dumps(
                {
                    "approved": not forbidden,
                    "violations": ["backend_information"] if forbidden else [],
                    "revision_guidance": "移除后台标签并使用自然、支持性的表达。"
                    if forbidden
                    else "",
                },
                ensure_ascii=False,
            )
        elif purpose == "response_repair":
            content = (
                "我听见你现在确实很难受。我们先从眼前的一小步开始："
                "去一个有人在场的地方，并联系一位你信任的人陪你聊一会儿。"
            )
        else:
            raise ModelCallError("UNSUPPORTED_PURPOSE", purpose)

        return ModelResponse(
            content=content,
            model=model,
            latency_ms=max(1, int((time.perf_counter() - started) * 1000)),
            input_tokens=max(1, sum(len(item.content) for item in messages) // 4),
            output_tokens=max(1, len(content) // 4),
            request_id=f"mock-{purpose}",
        )

    def stream(
        self,
        messages: list[ModelMessage],
        *,
        model: str,
        temperature: float,
        purpose: str,
    ) -> Iterator[str]:
        response = self.complete(
            messages,
            model=model,
            temperature=temperature,
            json_mode=False,
            purpose=purpose,
        )
        text = response.content
        for start in range(0, len(text), 18):
            yield text[start : start + 18]


class OpenAICompatibleModelProvider:
    name = "openai_compatible"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ):
        self.base_url = settings.model_base_url.rstrip("/")
        self.api_key = settings.model_api_key
        self.timeout = settings.model_timeout_seconds
        self.max_retries = max(0, settings.model_max_retries)
        self.json_mode_enabled = settings.model_json_mode
        self.client = client or httpx.Client(timeout=self.timeout)

    def complete(
        self,
        messages: list[ModelMessage],
        *,
        model: str,
        temperature: float,
        json_mode: bool,
        purpose: str,
    ) -> ModelResponse:
        if not self.api_key:
            raise ModelCallError(
                "MODEL_API_KEY_MISSING",
                "MODEL_API_KEY is required for the configured provider",
            )

        payload: dict = {
            "model": model,
            "messages": [
                {"role": item.role, "content": item.content} for item in messages
            ],
            "temperature": temperature,
            "stream": False,
        }
        if json_mode and self.json_mode_enabled:
            payload["response_format"] = {"type": "json_object"}

        last_error: ModelCallError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "X-MindNook-Purpose": purpose,
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
                    raise ModelCallError(
                        f"MODEL_HTTP_{response.status_code}",
                        f"model request failed with HTTP {response.status_code}",
                        retryable=retryable,
                    )
                body = response.json()
                content = str(body["choices"][0]["message"]["content"]).strip()
                usage = body.get("usage") or {}
                return ModelResponse(
                    content=content,
                    model=str(body.get("model") or model),
                    latency_ms=max(1, int((time.perf_counter() - started) * 1000)),
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    request_id=response.headers.get("x-request-id")
                    or body.get("id"),
                )
            except ModelCallError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.max_retries:
                    raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = ModelCallError(
                    "MODEL_NETWORK_ERROR",
                    type(exc).__name__,
                    retryable=True,
                )
                if attempt >= self.max_retries:
                    raise last_error from exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ModelCallError(
                    "MODEL_RESPONSE_INVALID",
                    "model response did not match the OpenAI-compatible schema",
                ) from exc

            time.sleep(min(0.25 * (2**attempt), 1.0))

        raise last_error or ModelCallError("MODEL_UNKNOWN_ERROR", "unknown model error")

    def stream(
        self,
        messages: list[ModelMessage],
        *,
        model: str,
        temperature: float,
        purpose: str,
    ) -> Iterator[str]:
        if not self.api_key:
            raise ModelCallError(
                "MODEL_API_KEY_MISSING",
                "MODEL_API_KEY is required for the configured provider",
            )
        payload = {
            "model": model,
            "messages": [
                {"role": item.role, "content": item.content} for item in messages
            ],
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        try:
            with self.client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "X-MindNook-Purpose": purpose,
                },
                json=payload,
                timeout=self.timeout,
            ) as response:
                if response.status_code >= 400:
                    raise ModelCallError(
                        f"MODEL_HTTP_{response.status_code}",
                        f"model stream failed with HTTP {response.status_code}",
                        retryable=False,
                    )
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        body = json.loads(data)
                        choices = body.get("choices") or []
                        if not choices:
                            # OpenAI-compatible providers may send a final usage-only
                            # event before [DONE]. It contains no text delta.
                            continue
                        delta = (choices[0].get("delta") or {}).get("content")
                    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                        raise ModelCallError(
                            "MODEL_STREAM_INVALID",
                            "model stream contained an invalid event",
                        ) from exc
                    if delta:
                        yield str(delta)
        except ModelCallError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ModelCallError(
                "MODEL_NETWORK_ERROR",
                type(exc).__name__,
                retryable=False,
            ) from exc

    def close(self) -> None:
        self.client.close()


# Backwards-compatible name used by older imports.
BailianModelProvider = OpenAICompatibleModelProvider


def build_model_provider(settings: Settings) -> ModelProvider:
    provider = settings.model_provider.lower()
    if provider in {"bailian", "openai_compatible", "openai-compatible"}:
        return OpenAICompatibleModelProvider(settings)
    return MockModelProvider()
