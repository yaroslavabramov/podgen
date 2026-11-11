"""One submit per invocation. Lost acknowledgements are never silently retried."""

import json
import re
import time

from podgen.runtime.evaluation.compiler import COUNT_FIELDS
from podgen.runtime.evaluation.contracts import RunFailure

from .contracts import ProviderFault


class ModelGateway:
    def __init__(
        self,
        journal,
        provider_factory,
        *,
        sleep=time.sleep,
        poll_seconds=2,
        source_corpus=None,
        total_input_ceiling=None,
    ):
        self.journal, self.provider_factory = journal, provider_factory
        self.sleep, self.poll_seconds = sleep, poll_seconds
        self.source_corpus = source_corpus
        self.total_input_ceiling = total_input_ceiling

    def _fail(self, invocation, code, *, status="failed"):
        self.journal.event(invocation, "failure", data=dict(code=code, status=status))
        self.journal.transition(invocation, status)
        raise RunFailure(code, run_id=invocation["run_id"], invocation_id=invocation["invocation_id"])

    def _fault(self, invocation, fault, operation):
        self.journal.event(
            invocation,
            "provider_error",
            reply=fault.reply,
            data=dict(code=fault.code, ambiguous=fault.ambiguous, operation=operation),
        )
        self.journal.transition(invocation, "outcome_unknown" if fault.ambiguous else "failed")
        raise RunFailure(
            fault.code, run_id=invocation["run_id"], invocation_id=invocation["invocation_id"]
        ) from None

    def invoke(self, invocation, profile, *, limits, totals, guard):
        from podgen.runtime.evaluation.technical_recovery import validate_stage

        if invocation.get("attempt_index", 0):
            validate_stage(self.journal, invocation)
        if invocation["status"] == "completed":
            return json.dumps(self.journal.output(invocation)["output"], ensure_ascii=False)
        from podgen.runtime.evaluation.source_tool_contract import applies, from_manifest

        contract = from_manifest(self.journal.manifest(invocation["run_id"]))
        if (
            contract is not None
            and invocation["operation_kind"] == "role"
            and applies(invocation["stage"], contract)
        ):
            from podgen.domain.errors import ContractError

            from .tool_gateway import invoke_tools

            try:
                return invoke_tools(
                    self, invocation, profile, contract, limits=limits, totals=totals, guard=guard
                )
            except ContractError as error:
                self._fail(invocation, error.code, status="validation_failed")
        return self.invoke_once(invocation, profile, limits=limits, totals=totals, guard=guard)

    def invoke_once(
        self, invocation, profile, *, limits, totals, guard, full_response=False, input_limits=None
    ):
        j = self.journal
        if invocation["status"] == "completed":
            return json.dumps(j.output(invocation)["output"], ensure_ascii=False)
        if invocation["status"] in ("failed", "validation_failed", "outcome_unknown"):
            raise RunFailure(
                "invocation_requires_new_attempt_or_reconciliation",
                run_id=invocation["run_id"],
                invocation_id=invocation["invocation_id"],
            )
        body = j.request(invocation)
        from podgen.domain.contracts.common import RuntimeRef
        from podgen.domain.errors import ContractError
        from podgen.runtime.evaluation.wire_budget import check_tokens, check_visible, visible_sizes

        record = j.read(RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"]))
        contract = record["context"].get("role_input_contract")
        manifest = j.manifest(invocation["run_id"])
        expected_contract = manifest.get("input_contract")
        if expected_contract == "editorial-role-packets/1" and contract != expected_contract:
            self._fail(invocation, "role_input_contract_changed", status="validation_failed")
        if expected_contract == "editorial-role-packets/1" and "role_projection_version" in manifest:
            if record["context"].get("role_projection_version") != manifest["role_projection_version"]:
                self._fail(invocation, "role_projection_version_mismatch", status="validation_failed")
        from podgen.runtime.evaluation.input_budget_recovery import effective_limits

        local_limits = (
            input_limits if input_limits is not None else effective_limits(j, invocation, manifest=manifest)
        )
        if contract == "editorial-role-packets/1" and invocation["status"] == "prepared":
            j.event(
                invocation,
                "role_input_measurement",
                data=dict(
                    visible_bytes=visible_sizes(body),
                    limits=dict(**local_limits),
                ),
            )
        try:
            check_visible(body, contract, max_visible_bytes=local_limits["visible_bytes"])
        except ContractError as error:
            self._fail(invocation, error.code, status="validation_failed")
        provider = self.provider_factory(profile)
        if invocation["status"] == "prepared":
            guard()
            count_body = {key: value for key, value in body.items() if key in COUNT_FIELDS}
            count_blob = j.blob(
                json.dumps(count_body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            )
            j.event(invocation, "count_intent", data={"request_blob": count_blob.model_dump(mode="json")})
            try:
                reply = provider.count(count_body)
            except ProviderFault as fault:
                self._fault(invocation, fault, "count")
            j.event(invocation, "count_response", reply=reply)
            try:
                count = json.loads(reply.body)["input_tokens"]
                if type(count) is not int or count < 0:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                self._fail(invocation, "invalid_input_token_count")
            invocation = j.transition(invocation, "counted", input_tokens=count)
        if invocation["status"] == "counted":
            try:
                check_visible(body, contract, max_visible_bytes=local_limits["visible_bytes"])
                check_tokens(
                    invocation["input_tokens"], contract, max_input_tokens=local_limits["input_tokens"]
                )
            except ContractError as error:
                self._fail(invocation, error.code, status="validation_failed")
            if invocation["input_tokens"] + profile.output_limit > profile.context_limit:
                self._fail(invocation, "context_capacity_exceeded", status="validation_failed")
            if (
                totals["input"] + invocation["input_tokens"]
                > (
                    self.total_input_ceiling
                    if self.total_input_ceiling is not None
                    else limits.max_total_input_tokens
                )
                or totals["output"] + profile.output_limit > limits.max_total_output_tokens
            ):
                self._fail(invocation, "run_token_budget_exceeded", status="validation_failed")
            guard()
            j.event(
                invocation, "submit_intent", data=dict(client_request_id=str(invocation["invocation_id"]))
            )
            invocation = j.transition(invocation, "submitting")
            try:
                reply = provider.submit(body, client_request_id=str(invocation["invocation_id"]))
            except ProviderFault as fault:
                self._fault(invocation, fault, "submit")
            raw = self._archive_reply(
                invocation, reply, body, expected_id=invocation.get("provider_response_id")
            )
        else:
            raw = j.last_response(invocation)
            if raw is None:
                self._fail(invocation, "submission_acknowledgement_unknown", status="outcome_unknown")
        return self._receive(invocation, profile, body, provider, raw, guard, full_response=full_response)

    def _receive(
        self, invocation, profile, body, provider, raw, guard, *, full_response=False, reconciled=False
    ):
        """Validate a submitted response identically on initial receipt and reconciliation."""
        j = self.journal
        deadline = time.monotonic() + float(profile.timeout_policy.get("poll_deadline_seconds", 1800))
        while True:
            try:
                response = json.loads(raw)
            except (ValueError, TypeError):
                self._fail(invocation, "provider_malformed_response", status="outcome_unknown")
            if not isinstance(response, dict):
                self._fail(invocation, "provider_malformed_response", status="outcome_unknown")
            binding_error = self._binding_error(response, body, invocation.get("provider_response_id"))
            if binding_error:
                self._fail(invocation, binding_error)
            status = response.get("status")
            response_id = response.get("id")
            if not isinstance(response_id, str) or not re.fullmatch(r"resp_[A-Za-z0-9_-]+", response_id):
                self._fail(invocation, "provider_response_id_missing", status="outcome_unknown")
            invocation = dict(invocation, provider_response_id=response_id)
            if status == "completed":
                text = None if full_response else self._output_text(invocation, response)
                usage = response.get("usage") or {}
                if not isinstance(usage, dict):
                    self._fail(invocation, "provider_invalid_usage")
                for name in ("input_tokens", "output_tokens"):
                    value = usage.get(name)
                    if value is not None and (type(value) is not int or value < 0):
                        self._fail(invocation, "provider_invalid_usage")
                j.transition(
                    invocation,
                    "response_completed",
                    provider_id=response_id,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                )
                return response if full_response else text
            if not isinstance(status, str):
                self._fail(invocation, "provider_malformed_status")
            if status not in ("queued", "in_progress"):
                code = {
                    "incomplete": "provider_incomplete",
                    "failed": "provider_failed",
                    "cancelled": "provider_cancelled",
                }.get(status, "provider_unknown_status")
                self._fail(invocation, code)
            if not reconciled and not profile.generation_parameters.get("store"):
                self._fail(invocation, "response_not_retrievable", status="outcome_unknown")
            if time.monotonic() >= deadline:
                j.transition(invocation, "waiting", provider_id=response_id)
                raise RunFailure(
                    "provider_poll_deadline",
                    run_id=invocation["run_id"],
                    invocation_id=invocation["invocation_id"],
                )
            guard()
            self.sleep(self.poll_seconds)
            try:
                reply = provider.retrieve(response_id)
            except ProviderFault as fault:
                # A failed read must not turn a known submitted generation into an unsent failure.
                j.event(invocation, "retrieve_error", reply=fault.reply, data=dict(code=fault.code))
                j.transition(invocation, "waiting", provider_id=response_id)
                raise RunFailure(
                    fault.code, run_id=invocation["run_id"], invocation_id=invocation["invocation_id"]
                ) from None
            raw = self._archive_reply(invocation, reply, body, expected_id=response_id)

    def _archive_reply(self, invocation, reply, body, expected_id=None):
        provider_id = None
        try:
            value = json.loads(reply.body)
            candidate = value.get("id") if isinstance(value, dict) else None
            if (
                isinstance(candidate, str)
                and re.fullmatch(r"resp_[A-Za-z0-9_-]+", candidate)
                and not self._binding_error(value, body, expected_id)
            ):
                provider_id = candidate
        except ValueError:
            pass
        self.journal.event(
            invocation, "provider_response", reply=reply, data={"provider_response_id": provider_id}
        )
        return reply.body

    def _binding_error(self, response, request, expected_id):
        if response.get("model") != request["model"]:
            return "provider_model_revision_mismatch"
        if expected_id is not None and response.get("id") != expected_id:
            return "provider_response_id_mismatch"
        metadata = response.get("metadata")
        if not isinstance(metadata, dict) or any(
            metadata.get(k) != v for k, v in request["metadata"].items()
        ):
            return "provider_response_metadata_mismatch"
        return None

    def _output_text(self, invocation, response):
        output = response.get("output")
        if not isinstance(output, list):
            self._fail(invocation, "provider_output_missing")
        parts = []
        for item in output:
            if not isinstance(item, dict):
                self._fail(invocation, "provider_malformed_output")
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                self._fail(invocation, "unexpected_provider_tool_or_output")
            if item.get("status") not in (None, "completed"):
                self._fail(invocation, "provider_message_incomplete")
            contents = item.get("content")
            if not isinstance(contents, list):
                self._fail(invocation, "provider_malformed_content")
            for content in contents:
                if not isinstance(content, dict):
                    self._fail(invocation, "provider_malformed_content")
                if content.get("type") == "refusal":
                    self._fail(invocation, "provider_refused")
                if content.get("type") != "output_text" or not isinstance(content.get("text"), str):
                    self._fail(invocation, "provider_output_missing")
                parts.append(content["text"])
        if not parts:
            self._fail(invocation, "provider_output_missing")
        return "".join(parts)
