"""One paid submission per immutable commission; resume polls the known job."""

import os
from datetime import datetime, timezone

from podgen.domain.canonical import canonical_bytes, digest
from podgen.domain.errors import Conflict, ContractError

from .contracts import TERMINAL_STATUSES, Approval, ResearchConfig
from .material import report
from .provider import prepare as prepare_request
from .state import exchanges, latest, status


def now():
    return datetime.now(timezone.utc).isoformat()


def prepare(journal, config):
    if journal.run_id != config.run_id:
        raise ContractError("hosted_research_run_mismatch")
    with journal.lock():
        existing = journal.get("manifest")
        raw = config.model_dump(mode="json")
        if existing:
            if existing["config"] != raw:
                raise Conflict("hosted_research_run_input_changed")
            return existing
        episode = None
        if config.episode_spec_ref:
            if config.episode_spec_ref.kind != "EpisodeSpec":
                raise ContractError("hosted_research_episode_spec_ref_required")
            episode = journal.repo.get(journal.project, config.episode_spec_ref).payload
            if episode["source_mode"] != "open_web":
                raise ContractError("hosted_research_open_web_not_authorized")
        request = prepare_request(config)
        manifest = dict(
            contract="hosted-research-manifest/1",
            config=raw,
            episode_spec=episode,
            request=request,
            request_hash=digest(request),
            request_blob=journal.blob(canonical_bytes(request)),
            created_at=now(),
        )
        journal.put("manifest", manifest)
        return manifest


def manifest(journal):
    value = journal.get("manifest")
    if not value or digest(value["request"]) != value["request_hash"]:
        raise ContractError("hosted_research_manifest_missing_or_changed")
    if journal.bytes("manifest", value["request_blob"]) != canonical_bytes(value["request"]):
        raise ContractError("hosted_research_request_bytes_changed")
    return value


def approve(journal, approval):
    with journal.lock():
        value = manifest(journal)
        if approval.request_hash != value["request_hash"]:
            raise ContractError("hosted_research_approval_request_mismatch")
        saved = journal.get("approval")
        raw = approval.model_dump(mode="json")
        if saved:
            if saved["approval"] != raw:
                raise Conflict("hosted_research_approval_changed")
            return saved
        saved = dict(
            approval=raw,
            actor_id=str(journal.repo.actor_id),
            approved_at=now(),
            manifest_ref=journal.ref("manifest").model_dump(mode="json"),
        )
        journal.put("approval", saved)
        return saved


def authorized(journal):
    value, approval = manifest(journal), journal.get("approval")
    if not approval:
        raise ContractError("hosted_research_requires_approval")
    checked = Approval.model_validate(approval["approval"])
    if checked.request_hash != value["request_hash"]:
        raise ContractError("hosted_research_approval_request_mismatch")
    return value


def process(journal, current):
    if journal.get("result"):
        return status(journal)
    key, value = current
    if value["status"] != "completed":
        return status(journal)
    material = report(value)
    receipt = journal.get(key + ":receipt")
    journal.put(
        "result",
        dict(
            contract="hosted-research-result/1",
            provider_job_id=value["id"],
            manifest_ref=journal.ref("manifest").model_dump(mode="json"),
            receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            raw_response_blob=receipt["raw_response_blob"],
            report_text_blob=journal.blob(material.pop("text").encode(), "text/markdown"),
            material=material,
            usage=value.get("usage"),
            status="completed",
            cost_usd=None,
            cost_basis="provider_invoice_not_available",
        ),
    )
    return status(journal)


def advance(journal, provider, *, operation="resume"):
    if operation not in ("resume", "cancel", "reprocess"):
        raise ValueError("invalid_research_operation")
    with journal.lock():
        value = authorized(journal)
        current = latest(journal)
        if operation == "reprocess":
            if not current or current[1]["status"] != "completed":
                raise ContractError("hosted_research_no_completed_receipt")
            return process(journal, current)
        if current and current[1]["status"] in TERMINAL_STATUSES:
            return process(journal, current)
        previous = list(exchanges(journal))
        if not current and previous:
            return status(journal)  # Unknown submit outcome must never trigger another POST.
        if operation == "cancel" and not current:
            raise ContractError("hosted_research_cancel_requires_known_job")
        profile = ResearchConfig.model_validate(value["config"]).profile
        credential = os.environ.get(profile.credential_env)
        if not credential:
            return {**status(journal), "operation_error": "credential_missing"}
        action = "cancel" if operation == "cancel" else "get" if current else "submit"
        key = f"exchange:{len(previous) + 1}"
        journal.put(
            key + ":intent",
            dict(
                operation=action,
                created_at=now(),
                manifest_ref=journal.ref("manifest").model_dump(mode="json"),
                approval_ref=journal.ref("approval").model_dump(mode="json"),
                provider_job_id=current[1]["id"] if current else None,
            ),
        )
        request = value["request"]
        if action == "submit":
            response = provider.submit(request, credential)
        else:
            response = getattr(provider, action)(request, current[1]["id"], credential)
        journal.put(
            key + ":receipt",
            dict(
                contract="hosted-research-http/1",
                status_code=response.status_code,
                headers=response.headers,
                complete=response.complete,
                error=response.error,
                raw_response_blob=journal.blob(response.body),
                received_at=now(),
                intent_ref=journal.ref(key + ":intent").model_dump(mode="json"),
            ),
        )
        updated = latest(journal)
        if updated:
            job = dict(
                provider_job_id=updated[1]["id"], manifest_ref=journal.ref("manifest").model_dump(mode="json")
            )
            journal.put("job", job)
            result = process(journal, updated)
        else:
            result = status(journal)
        if (
            not response.complete
            or response.error
            or response.status_code is None
            or response.status_code >= 300
        ):
            result["operation_error"] = response.error or f"http_{response.status_code}"
        return result
