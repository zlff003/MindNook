from __future__ import annotations

from collections.abc import Iterator

from app.harness.agent_harness import AgentHarness, ChatCommand, HarnessEvent


class ChatService:
    """HTTP-facing application service for public chat events only."""

    def __init__(self, harness: AgentHarness):
        self.harness = harness

    def stream(self, command: ChatCommand) -> Iterator[HarnessEvent]:
        for event in self.harness.run(command):
            if event.public:
                yield event
