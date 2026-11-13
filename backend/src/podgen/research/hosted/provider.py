"""Gemini Interactions wire contract, pinned to the documented API revision."""

import json
from typing import Protocol
from urllib.parse import quote

from podgen.domain.errors import ContractError

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"


class HostedResearchProvider(Protocol):
    def submit(self, prepared: dict, credential: str): ...

    def get(self, prepared: dict, job_id: str, credential: str): ...

    def cancel(self, prepared: dict, job_id: str, credential: str): ...


def prepare(config):
    assignment = json.dumps(config.assignment, ensure_ascii=False, indent=2)
    return dict(
        method="POST",
        url=ENDPOINT,
        headers={"Api-Revision": config.profile.api_revision, "Content-Type": "application/json"},
        body=dict(
            input=config.instructions.rstrip() + "\n\nUser payload:\n" + assignment + "\n",
            agent=config.profile.agent,
            background=True,
            store=True,
        ),
    )


def snapshot(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
        raise ContractError("hosted_research_missing_job_id")
    if value.get("status") not in {
        "in_progress",
        "completed",
        "failed",
        "cancelled",
        "incomplete",
        "requires_action",
        "requires_continuation",
    }:
        raise ContractError("hosted_research_unknown_provider_status")
    return value


class GeminiResearchProvider:
    def __init__(self, transport):
        self.transport = transport

    def submit(self, prepared, credential):
        return self.transport.request(
            "POST", prepared["url"], prepared["headers"], credential, prepared["body"]
        )

    def get(self, prepared, job_id, credential):
        return self.transport.request(
            "GET", ENDPOINT + "/" + quote(job_id, safe=""), prepared["headers"], credential
        )

    def cancel(self, prepared, job_id, credential):
        return self.transport.request(
            "POST", ENDPOINT + "/" + quote(job_id, safe="") + "/cancel", prepared["headers"], credential
        )
