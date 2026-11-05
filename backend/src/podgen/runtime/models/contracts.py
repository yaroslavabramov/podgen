from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WireReply:
    body: bytes
    status_code: int = 200
    request_id: str | None = None
    redacted: bool = False
    request_body: bytes | None = None


class ProviderFault(Exception):
    def __init__(self, code, *, ambiguous=False, reply: WireReply | None = None):
        self.code, self.ambiguous, self.reply = code, ambiguous, reply
        super().__init__(code)


class Provider(Protocol):
    def count(self, body: dict) -> WireReply: ...
    def submit(self, body: dict, *, client_request_id: str) -> WireReply: ...
    def retrieve(self, response_id: str) -> WireReply: ...
