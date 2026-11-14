"""One physical model step owns explicit publication phases and pending metadata."""

import json

from podgen.runtime.evaluation.contracts import RunFailure

from .contracts import ProviderFault


class ModelStep:
    def __init__(self, gateway, invocation, phase=None):
        self.gateway, self.journal, self.invocation = gateway, gateway.journal, invocation
        self.phase = phase or self.journal.publication(invocation)

    def publish(self, *, guard=None):
        self.invocation = self.phase.commit(guard=guard)
        self.phase = self.journal.publication(self.invocation)
        return self.invocation

    def fail(self, invocation, code, *, status="failed"):
        self.gateway._fail(invocation, code, status=status, phase=self.phase)

    def invoke(
        self,
        profile,
        *,
        limits,
        totals,
        guard,
        full_response=False,
        input_limits=None,
        count_receipt=None,
        request_snapshot=None,
        manifest_snapshot=None,
    ):
        invocation = self.invocation
        j = self.journal
        if invocation["status"] == "completed":
            return json.dumps(j.output(invocation)["output"], ensure_ascii=False)
        if invocation["status"] in ("failed", "validation_failed", "outcome_unknown"):
            raise RunFailure(
                "invocation_requires_new_attempt_or_reconciliation",
                run_id=invocation["run_id"],
                invocation_id=invocation["invocation_id"],
            )
        from podgen.domain.contracts.common import RuntimeRef
        from podgen.domain.errors import AccessDenied, ContractError
        from podgen.runtime.evaluation.wire_budget import check_tokens, check_visible, visible_sizes

        step = self
        pending_receipt = getattr(count_receipt, "phase", None)
        receipt_in_publication = pending_receipt is not None and not pending_receipt.finished
        if receipt_in_publication:
            step.phase.absorb(pending_receipt, prepend=True)
        try:
            with j.read_operation():
                if request_snapshot is None:
                    body = j.request(invocation)
                    record = j.read(
                        RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
                    )
                else:
                    body, record = request_snapshot
                manifest = (
                    manifest_snapshot if manifest_snapshot is not None else j.manifest(invocation["run_id"])
                )
                if invocation["status"] == "counted" and count_receipt is None:
                    from .direct_count import restored

                    count_receipt = restored(j, invocation, body, profile)
                reused_count = count_receipt.validate(j, body, profile) if count_receipt is not None else None
                if invocation["status"] == "counted":
                    if reused_count != invocation["input_tokens"]:
                        raise ContractError("count_receipt_binding_changed")
                    step.phase.event(
                        "count_receipt_reused",
                        data=dict(
                            count_ref=count_receipt.reference.model_dump(mode="json"),
                            count_identity=count_receipt.identity,
                            input_tokens=reused_count,
                        ),
                    )
            register = getattr(count_receipt, "register", None)
            if count_receipt is not None and not receipt_in_publication:
                j.execution.prepare(invocation["run_id"], evaluation=[count_receipt.reference])
            if register is not None:
                register(j, step.phase)
            contract = record["context"].get("role_input_contract")
            from .provider_contracts import validate as validate_provider_contract

            validate_provider_contract(
                manifest, body["metadata"].get("podgen_stage", invocation.get("stage")), profile
            )
            expected_contract = manifest.get("input_contract")
            if expected_contract == "editorial-role-packets/1" and contract != expected_contract:
                step.fail(invocation, "role_input_contract_changed", status="validation_failed")
            if expected_contract == "editorial-role-packets/1" and "role_projection_version" in manifest:
                if record["context"].get("role_projection_version") != manifest["role_projection_version"]:
                    step.fail(invocation, "role_projection_version_mismatch", status="validation_failed")
            from podgen.runtime.evaluation.input_budget_recovery import effective_limits

            local_limits = (
                input_limits
                if input_limits is not None
                else effective_limits(j, invocation, manifest=manifest)
            )
            from podgen.runtime.evaluation.wire_budget import resolved_limits

            local_limits = resolved_limits(local_limits, profile)
        except AccessDenied:
            raise
        except (ContractError, RunFailure) as error:
            if step.phase.finished:
                raise
            step.phase.event("failure", data=dict(code=error.code, status="validation_failed"))
            step.phase.transition("validation_failed")
            step.publish()
            raise
        if contract == "editorial-role-packets/1" and invocation["status"] == "prepared":
            step.phase.event(
                "role_input_measurement",
                data=dict(
                    visible_bytes=visible_sizes(body),
                    limits=dict(**local_limits),
                ),
            )
        try:
            check_visible(body, contract, max_visible_bytes=local_limits["visible_bytes"])
        except ContractError as error:
            step.fail(invocation, error.code, status="validation_failed")
        try:
            provider = (
                self.gateway.provider_factory(profile)
                if invocation["status"] in ("prepared", "counted")
                else None
            )
        except ProviderFault as fault:
            self.gateway._fault(invocation, fault, "provider_initialization", phase=step.phase)
        except AccessDenied:
            raise
        except (ContractError, RunFailure) as error:
            step.phase.event("failure", data=dict(code=error.code, status="validation_failed"))
            step.phase.transition("validation_failed")
            step.publish()
            raise
        if invocation["status"] == "prepared":
            if reused_count is not None:
                count = reused_count
                step.phase.event(
                    "count_receipt_reused",
                    data=dict(
                        count_ref=count_receipt.reference.model_dump(mode="json"),
                        count_identity=count_receipt.identity,
                        input_tokens=count,
                    ),
                )
            else:
                from .direct_count import capture, prepare

                admission_refs = getattr(guard, "admission_refs", lambda: [])()
                count_body, intent, native_blob, count_data = prepare(
                    step.phase, body, profile, admission_refs
                )
                invocation = step.publish(guard=guard)
                try:
                    reply = provider.count(count_body)
                except ProviderFault as fault:
                    self.gateway._fault(invocation, fault, "count")
                try:
                    count = capture(step.phase, intent, native_blob, count_data, reply, body, profile)
                except ContractError as error:
                    step.fail(invocation, error.code)
            step.phase.transition("counted", input_tokens=count)
            invocation = dict(invocation, status="counted", input_tokens=count)
        if invocation["status"] == "counted":
            try:
                check_visible(body, contract, max_visible_bytes=local_limits["visible_bytes"])
                check_tokens(
                    invocation["input_tokens"], contract, max_input_tokens=local_limits["input_tokens"]
                )
            except ContractError as error:
                step.fail(invocation, error.code, status="validation_failed")
            if invocation["input_tokens"] + profile.output_limit > profile.context_limit:
                step.fail(invocation, "context_capacity_exceeded", status="validation_failed")
            if (
                totals["input"] + invocation["input_tokens"] > 2**63 - 1
                or totals["output"] + profile.output_limit > 2**63 - 1
            ):
                step.fail(invocation, "run_token_usage_overflow", status="validation_failed")
            if (
                totals["input"] + invocation["input_tokens"]
                > (
                    self.gateway.total_input_ceiling
                    if self.gateway.total_input_ceiling is not None
                    else limits.max_total_input_tokens
                )
                or totals["output"] + profile.output_limit > limits.max_total_output_tokens
            ):
                step.fail(invocation, "run_token_budget_exceeded", status="validation_failed")
            step.phase.event(
                "submit_intent",
                data=dict(
                    client_request_id=str(invocation["invocation_id"]), output_reserved=profile.output_limit
                ),
            )
            step.phase.transition("submitting", input_tokens=invocation["input_tokens"])
            try:
                invocation = step.publish(guard=guard)
            except Exception:
                # A received count remains replayable when dispatch authorization
                # fails. Capturing it still performs current evaluator read checks.
                # No submit intent may survive this unsuccessful dispatch boundary.
                step.phase.capture_count_after_dispatch_failure(invocation["input_tokens"])
                raise
            try:
                reply = provider.submit(body, client_request_id=str(invocation["invocation_id"]))
            except ProviderFault as fault:
                self.gateway._fault(invocation, fault, "submit")
            raw = self.gateway._archive_reply(
                invocation, reply, body, expected_id=invocation.get("provider_response_id")
            )
        else:
            raw = j.last_response(invocation)
            if raw is None:
                step.fail(invocation, "submission_acknowledgement_unknown", status="outcome_unknown")
        return self.gateway._receive(
            invocation, profile, body, provider, raw, guard, full_response=full_response
        )
