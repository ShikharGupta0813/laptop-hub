"""The voice/text assistant that can act on the laptop. (Placeholder: being written.)

Contract the server relies on:

    service = AssistantService(config, claude=None, local=None)
    async for event in service.run(session_id, text, provider, auto_approve): ...
    service.resolve(session_id, call_id, approve) -> bool
    service.reset(session_id) -> None
"""
from __future__ import annotations


class AssistantService:
    def __init__(self, config, claude=None, local=None):
        self.config = config

    async def run(self, session_id: str, text: str, provider: str, auto_approve: bool):
        yield {"type": "error", "message": "The assistant is not implemented yet."}

    def resolve(self, session_id: str, call_id: str, approve: bool) -> bool:
        return False

    def reset(self, session_id: str) -> None:
        pass
