"""Conservative, explicitly estimated budgets for arbitrary compatible models.

UTF-8 bytes deliberately overestimate common byte/subword tokenizers. A provider's
actual usage remains the source of truth for accounting; this is not an exact tokenizer.
"""
from __future__ import annotations

from app.services.model import ModelCallError


def estimate_tokens(text: str) -> int:
    return len(text.encode("utf-8"))


def message_tokens(messages) -> int:
    return 8 + sum(estimate_tokens(item.content) + 8 for item in messages)


def output_limit(settings, model: str, *, memory: bool = False) -> int:
    default = settings.memory_model_output_tokens if memory else settings.model_output_tokens
    return settings.model_context_limits.get(model, {}).get("output_tokens", default)


def input_limit(settings, model: str, *, memory: bool = False) -> int:
    capacity = settings.model_context_limits.get(model, {}).get("context_tokens", settings.model_context_tokens)
    return max(0, capacity - output_limit(settings, model, memory=memory) - settings.model_context_margin_tokens)


def check_budget(settings, messages, model: str, *, memory: bool = False) -> int:
    count = message_tokens(messages)
    if count > input_limit(settings, model, memory=memory):
        raise ModelCallError("CONTEXT_BUDGET_EXCEEDED", "model input exceeds configured token budget")
    return count
