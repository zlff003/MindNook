from __future__ import annotations

import json
import re
import time

from app.memory.prompts import FACT_EXTRACTION_PROMPT, CORRECTION_PROMPT, CORRECTION_VERIFY_PROMPT
from app.memory.repository import memory_dict
from app.memory.types import FactExtraction, CorrectionProposal, CorrectionVerification, MemoryUpdate, MemoryOperation, is_stable_preference
from app.services.context_budget import check_budget
from app.services.model import ModelMessage
from app.services.privacy import redact_pii
from app.models import Message


class SummaryGenerator:
    """Extract quotations or propose verified corrections; never rewrite a summary."""
    def __init__(self, settings, provider):
        self.settings, self.provider = settings, provider

    def generate(self, db, conversation, summary, messages, related, *, mode="compact", focus="", current=None):
        model = self.settings.memory_model_name or self.settings.model_name
        usage = {"purpose": f"memory_{mode}", "model": model, "status": "failed",
                 "input_tokens": 0, "output_tokens": 0, "requests": []}
        started = time.perf_counter()

        def call(prompt, payload, contract, purpose):
            request = [ModelMessage("system", prompt), ModelMessage("user", json.dumps(payload, ensure_ascii=False))]
            check_budget(self.settings, request, model, memory=True)
            response = self.provider.complete(request, model=model, temperature=0, json_mode=True, purpose=purpose)
            usage["input_tokens"] += response.input_tokens
            usage["output_tokens"] += response.output_tokens
            usage["usage_source"] = "estimated" if self.provider.name == "mock" else "provider"
            usage["requests"].append({"purpose": purpose, "request_id": response.request_id,
                                      "finish_reason": response.finish_reason})
            if response.finish_reason in ("length", "content_filter"):
                raise ValueError("incomplete model output: " + response.finish_reason)
            text = response.content.strip()
            if text.startswith("```"):
                text = "\n".join(text.splitlines()[1:-1])
            return contract.model_validate_json(text)

        try:
            if mode == "correct":
                if current is None or current.sender_type != "student":
                    raise ValueError("correction requires a student message")
                allowed = {current.public_id: current}
                payload = {"mode": "correct", "current_message": {"message_id": current.public_id,
                    "content": redact_pii(current.redacted_content or current.content)},
                    "related_memories": [memory_dict(row) for row in related]}
                proposal = call(CORRECTION_PROMPT, payload, CorrectionProposal, "memory_correct")
                targets = {row.public_id: row for row in related}
                if any(p.memory_id not in targets for p in proposal.corrections):
                    raise ValueError("unauthorized correction target")
                # The model chooses targets only. Evidence and replacement text are bound by code.
                result = MemoryUpdate(memory_operations=[MemoryOperation(
                    action="update", memory_id=p.memory_id, expected_version=p.expected_version,
                    content=payload["current_message"]["content"], quote=payload["current_message"]["content"],
                    target_quote=targets[p.memory_id].content, source_message_ids=[current.public_id],
                    type=targets[p.memory_id].type, topics=json.loads(targets[p.memory_id].topics_json),
                    subject="uncertain", stage="feedback" if p.change == "evolution" else "reported", change=p.change)
                    for p in proposal.corrections],
                                      needs_clarification=proposal.needs_clarification)
                self.validate(result, allowed, related, mode="correct")
                usage["needs_clarification"] = proposal.needs_clarification
                if proposal.needs_clarification:
                    raise ValueError("correction needs clarification")
                if result.memory_operations:
                    targets = {row.public_id: row for row in related}
                    verification = call(CORRECTION_VERIFY_PROMPT, {
                        "mode": "verify", "current_message": payload["current_message"],
                        "proposals": [{"operation": op.model_dump(), "old_fact": memory_dict(targets[op.memory_id])}
                                      for op in result.memory_operations]}, CorrectionVerification, "memory_verify")
                    expected = {op.memory_id for op in result.memory_operations}
                    if (len(verification.decisions) != len(expected) or
                        {v.memory_id for v in verification.decisions} != expected):
                        raise ValueError("correction not independently supported")
                    approved = {v.memory_id for v in verification.decisions if v.approved}
                    if not approved:
                        raise ValueError("correction not independently supported")
                    usage["verified_targets"] = sorted(approved)
                    usage["unresolved_targets"] = sorted(expected - approved)
                    result.memory_operations = [op for op in result.memory_operations if op.memory_id in approved]
            else:
                # No focus, old summaries or related bodies: later facts cannot leak into this batch.
                allowed = {msg.message_id: db.get(Message, msg.db_id) for msg in messages}
                payload = {"mode": mode, "messages": [
                    {"message_id": msg.message_id, "role": msg.role, "content": msg.content} for msg in messages]}
                facts = call(FACT_EXTRACTION_PROMPT, payload, FactExtraction, f"memory_{mode}")
                if (len(facts.reviewed_message_ids) != len(allowed) or
                    set(facts.reviewed_message_ids) != set(allowed)):
                    raise ValueError("batch not fully reviewed")
                operations, seen = [], set()
                for f in facts.facts:
                    source = allowed.get(f.message_id)
                    if source is None or f.quote not in redact_pii(source.redacted_content or source.content):
                        raise ValueError("invented or unauthorized source quotation")
                    # Split only at sentence boundaries; never at commas that can separate negation/attribution.
                    for quote in re.findall(r"[^。！？!?；;\n]+[。！？!?；;\n]*", f.quote):
                        quote = quote.strip()
                        if not quote or (f.message_id, quote) in seen:
                            continue
                        seen.add((f.message_id, quote))
                        operations.append(MemoryOperation(content=quote, quote=quote, source_message_ids=[f.message_id],
                            type="episode" if f.type == "preference" and not is_stable_preference(quote) else f.type,
                            topics=f.topics, stage=f.stage, subject=f.subject))
                result = MemoryUpdate(memory_operations=operations)
                self.validate(result, allowed, [], mode=mode)
            usage.update(status="partial" if usage.get("unresolved_targets") else "succeeded",
                         needs_clarification=result.needs_clarification)
            return result, allowed, usage
        except Exception as exc:
            usage.update(error=type(exc).__name__, reason=str(exc)[:500])
            setattr(exc, "memory_usage", usage)
            raise
        finally:
            usage["latency_ms"] = int((time.perf_counter() - started) * 1000)

    @staticmethod
    def validate(result, allowed, related, *, mode="compact"):
        targets = {row.public_id: row for row in related}
        seen = set()
        for op in result.memory_operations:
            if len(op.source_message_ids) != 1 or not set(op.source_message_ids) <= allowed.keys():
                raise ValueError("invented or unauthorized source message")
            source = allowed[op.source_message_ids[0]]
            text = redact_pii(source.redacted_content or source.content)
            if not op.quote.strip() or op.quote not in text or op.content != op.quote:
                raise ValueError("fact must be an exact source quotation")
            start = text.index(op.quote)
            end = start + len(op.quote)
            boundaries = "。！？!?；;\n"
            prefix = text[:start].rstrip()
            if (prefix and prefix[-1] not in boundaries) or (
                end < len(text) and text[end] not in boundaries and op.quote[-1] not in boundaries):
                raise ValueError("quotation must preserve whole clauses including negation and attribution")
            if source.sender_type != "student" and (op.subject != "assistant" or op.stage != "suggested"):
                raise ValueError("user facts require student evidence")
            if mode != "correct":
                if op.action != "add" or op.memory_id or op.change != "new":
                    raise ValueError("extraction cannot mutate existing facts")
            else:
                target = targets.get(op.memory_id)
                if (op.action == "add" or target is None or target.version != op.expected_version or
                    op.change not in ("correction", "evolution") or op.target_quote != target.content or
                    op.memory_id in seen or (op.action == "retract" and op.change != "correction")):
                    raise ValueError("unauthorized correction target/version")
                seen.add(op.memory_id)
