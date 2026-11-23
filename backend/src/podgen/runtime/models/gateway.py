"""One submit per invocation. Lost acknowledgements are never silently retried."""

import json
import re
import time

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.contracts import RunFailure

from .contracts import ProviderFault
from .response_content import binding_error, output_text


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
        cost_limit_microusd=None,
    ):
        self.journal, self.provider_factory = journal, provider_factory
        self.sleep, self.poll_seconds = sleep, poll_seconds
        self.source_corpus = source_corpus
        self.total_input_ceiling = total_input_ceiling
        # An authorized run-cost grant; None uses the manifest's pinned limit.
        self.cost_limit_microusd = cost_limit_microusd

    def _fail(self, invocation, code, *, status="failed", phase=None):
        phase = phase or self.journal.publication(invocation)
        phase.event("failure", data=dict(code=code, status=status))
        phase.transition(status)
        phase.commit()
        raise RunFailure(code, run_id=invocation["run_id"], invocation_id=invocation["invocation_id"])

    def _fault(self, invocation, fault, operation, *, phase=None):
        phase = phase or self.journal.publication(invocation)
        phase.event(
            "provider_error",
            reply=fault.reply,
            data=dict(code=fault.code, ambiguous=fault.ambiguous, operation=operation),
        )
        from podgen.runtime.evaluation.provider_rejections import incoming

        phase.rejection = incoming(self.journal, invocation, fault, operation)
        phase.transition("outcome_unknown" if fault.ambiguous else "failed")
        phase.commit()
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

    def invoke_once(self, invocation, profile, *, prepared_phase=None, **options):
        from .model_step import ModelStep

        return ModelStep(self, invocation, prepared_phase).invoke(profile, **options)

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
            mismatch = binding_error(response, body, invocation.get("provider_response_id"))
            if mismatch:
                self._fail(invocation, mismatch)
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
                    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
                        self._fail(invocation, "provider_invalid_usage")
                if invocation["status"] != "response_completed":
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
                if provider is None:
                    provider = self.provider_factory(profile)
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
                and not binding_error(value, body, expected_id)
            ):
                provider_id = candidate
        except ValueError:
            pass
        phase = self.journal.publication(invocation)
        phase.event("provider_response", reply=reply, data={"provider_response_id": provider_id})
        if provider_id and value.get("status") == "completed":
            usage = value.get("usage") or {}
            if isinstance(usage, dict) and all(
                usage.get(name) is None or type(usage[name]) is int and 0 <= usage[name] <= 2**63 - 1
                for name in ("input_tokens", "output_tokens")
            ):
                cached = (
                    (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
                    if isinstance(usage.get("input_tokens_details", {}), dict)
                    else 0
                )
                if type(cached) is int and 0 <= cached <= (usage.get("input_tokens") or 0):
                    phase.cached_input_tokens = cached
                from podgen.runtime.evaluation import cost_budget

                budget = cost_budget.active(self.journal.manifest(invocation["run_id"]))
                if budget is not None and usage.get("output_tokens") is not None:
                    try:
                        phase.cost_microusd = cost_budget.charge(budget, body["model"], value)
                    except ContractError:
                        # The conservative submit reservation remains the recorded exposure.
                        pass
                phase.transition(
                    "response_completed",
                    provider_id=provider_id,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                )
        invocation.update(phase.commit())
        return reply.body

    def _output_text(self, invocation, response, *, web_search=False):
        try:
            return output_text(response, web_search=web_search)
        except ContractError as error:
            self._fail(invocation, error.code)
