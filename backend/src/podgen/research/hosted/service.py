"""One paid submission per immutable commission; resume polls the known job."""

import os
from datetime import datetime, timezone

from podgen.domain.canonical import canonical_bytes, digest
from podgen.domain.errors import Conflict, ContractError

from .contracts import TERMINAL_STATUSES, Approval, ResearchConfig
from .material import report
from .projection import intent_head, receipt_head
from .provider import prepare as prepare_request
from .state import head, latest, status


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
        journal.create_manifest(manifest)
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


def process(journal, current, *, projection=None):
    if journal.get("result"):
        return status(journal, projection=projection)
    key, value = current
    if value["status"] != "completed":
        return status(journal, projection=projection)
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
    return status(journal, projection=projection)


def advance(journal, provider, *, operation="resume"):
    if operation not in ("resume", "cancel", "reprocess"):
        raise ValueError("invalid_research_operation")
    with journal.lock():
        value = authorized(journal)
        saved = journal.ensure_job(head(journal, locked=True))
        if journal.get("result") and operation != "reprocess":
            return status(journal, projection=saved)
        current = latest(journal, projection=saved)
        if operation == "reprocess":
            if not current or current[1]["status"] != "completed":
                raise ContractError("hosted_research_no_completed_receipt")
            return process(journal, current, projection=saved)
        if current and current[1]["status"] in TERMINAL_STATUSES:
            return process(journal, current, projection=saved)
        if not current and saved["exchanges"]:
            return status(journal, projection=saved)  # Unknown submit outcome never triggers another POST.
        if operation == "cancel" and not current:
            raise ContractError("hosted_research_cancel_requires_known_job")
        profile = ResearchConfig.model_validate(value["config"]).profile
        credential = os.environ.get(profile.credential_env)
        if not credential:
            return {**status(journal, projection=saved), "operation_error": "credential_missing"}
        action = "cancel" if operation == "cancel" else "get" if current else "submit"
        from podgen.runtime.dispatch import before_external

        before_external("hosted_research_" + action, str(journal.run_id))
        key = f"exchange:{saved['exchanges'] + 1}"
        submitting = intent_head(saved, action)
        journal.exchange(
            key + ":intent",
            dict(
                operation=action,
                created_at=now(),
                manifest_ref=journal.ref("manifest").model_dump(mode="json"),
                approval_ref=journal.ref("approval").model_dump(mode="json"),
                provider_job_id=current[1]["id"] if current else None,
            ),
            saved,
            submitting,
        )
        request = value["request"]
        if action == "submit":
            response = provider.submit(request, credential)
        else:
            response = getattr(provider, action)(request, current[1]["id"], credential)
        receipt = dict(
            contract="hosted-research-http/1",
            status_code=response.status_code,
            headers=response.headers,
            complete=response.complete,
            error=response.error,
            raw_response_blob=journal.blob(response.body),
            received_at=now(),
            intent_ref=journal.ref(key + ":intent").model_dump(mode="json"),
        )
        captured = receipt_head(submitting, key, receipt, response.body)
        journal.exchange(key + ":receipt", receipt, submitting, captured)
        updated = latest(journal, projection=captured)
        if updated:
            result = process(journal, updated, projection=captured)
        else:
            result = status(journal, projection=captured)
        if (
            not response.complete
            or response.error
            or response.status_code is None
            or response.status_code >= 300
        ):
            result["operation_error"] = response.error or f"http_{response.status_code}"
        return result
