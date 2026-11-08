"""Eval-only Responses/Messages transport; no new production provider dependency."""

import os

import httpx
from artifacts import read, save, send_once
from conditions import response_text
from dotenv import dotenv_values

ENDPOINTS = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1"}


def credentials(root):
    values = dotenv_values(root / ".env")
    return {
        name: os.getenv(variable) or values.get(variable, "")
        for name, variable in (("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"))
    }


class Transport:
    def __init__(self, provider, key):
        if not key:
            raise ValueError("Missing API credential for " + provider)
        self.provider, self.key = provider, key

    @property
    def headers(self):
        if self.provider == "openai":
            return {"Authorization": "Bearer " + self.key}
        return {"x-api-key": self.key, "anthropic-version": "2023-06-01"}

    def models(self):
        rows, cursor = [], None
        with httpx.Client(timeout=60, transport=httpx.HTTPTransport(retries=0)) as client:
            while True:
                response = client.get(
                    ENDPOINTS[self.provider] + "/models",
                    headers=self.headers,
                    params={"after_id": cursor} if cursor else {},
                )
                if response.status_code != 200:
                    return dict(http_status=response.status_code, data=[], available=False)
                page = response.json()
                rows.extend(page["data"])
                if not page.get("has_more"):
                    break
                cursor = page["last_id"]
        return dict(http_status=200, data=rows, available=True)

    def __call__(self, body, client_id):
        headers = dict(self.headers)
        headers["X-Client-Request-Id"] = client_id
        endpoint = "/responses" if self.provider == "openai" else "/messages"
        with httpx.Client(timeout=900, transport=httpx.HTTPTransport(retries=0)) as client:
            response = client.post(ENDPOINTS[self.provider] + endpoint, headers=headers, json=body)
        raw = response.content.replace(self.key.encode(), b"[REDACTED_API_KEY]")
        return (
            response.status_code,
            response.headers.get("request-id") or response.headers.get("x-request-id"),
            raw,
        )


def probe_body(provider, model):
    if provider == "openai":
        return dict(
            model=model,
            input="Reply with OK only.",
            max_output_tokens=1024,
            reasoning={"effort": "medium"},
            store=False,
            stream=False,
            service_tier="default",
        )
    return dict(
        model=model,
        messages=[{"role": "user", "content": "Reply with OK only."}],
        max_tokens=1024,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
    )


def probe(directory, provider, model, transport):
    response = send_once(directory, probe_body(provider, model), transport)
    status = read(directory / "transport.json")["status"]
    if status == 200 and response.get("model") == model and response_text(response, provider).strip():
        return True
    error = response.get("error", {})
    if status == 400 and error.get("param") != "model":
        raise RuntimeError("Probe contract rejected; this does not establish model unavailability.")
    # Only a definite pre-generation refusal permits trying another model/provider.
    if status not in (400, 401, 403, 404):
        raise RuntimeError("Probe unresolved; no automatic fallback or resubmission.")
    return False


def select(directory, transports):
    selected_path = directory / "sealed/selected.json"
    if selected_path.exists():
        return read(selected_path)
    inventories = {}
    for provider, transport in transports.items():
        path = directory / ("sealed/" + provider + "-models.json")
        if not path.exists():
            save(path, transport.models())
        inventories[provider] = read(path)
    assert any(row["id"] == "gpt-6.1-sol" for row in inventories["openai"]["data"])
    assert probe(directory / "sealed/probes/sol-medium", "openai", "gpt-6.1-sol", transports["openai"])
    candidates = []
    tested_anthropic = False
    for line in ("opus", "sonnet"):
        rows = [
            row
            for row in inventories["anthropic"]["data"]
            if row.get("line") == line and row.get("lifecycle") == "active"
        ]
        # API-created dates, not guessed model IDs, determine the current candidate for each line.
        if rows:
            model = max(rows, key=lambda row: row["created_at"])["id"]
            tested_anthropic = True
            if probe(directory / ("sealed/probes/" + line), "anthropic", model, transports["anthropic"]):
                candidates.append(dict(provider="anthropic", model=model))
                break
    if not candidates:
        if not tested_anthropic:
            raise RuntimeError(
                "No listed Anthropic model was generation-tested; cannot infer a provider block."
            )
        assert any(row["id"] == "gpt-6.1-terra" for row in inventories["openai"]["data"])
        assert probe(directory / "sealed/probes/terra", "openai", "gpt-6.1-terra", transports["openai"])
        candidates.append(dict(provider="openai", model="gpt-6.1-terra"))
    result = [dict(provider="openai", model="gpt-6.1-sol"), *candidates]
    save(selected_path, result)
    return result
