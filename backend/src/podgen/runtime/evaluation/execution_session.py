"""One run's prepared inputs and expected coordinator revision; no permission cache."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.references import walk_values
from podgen.persistence.database import one, query
from podgen.persistence.execution.inputs import _publish, bind
from podgen.persistence.private_journal import access, state
from podgen.persistence.read_authorization import _read_connection


class ExecutionSession:
    def __init__(self, repo, project):
        self.repo, self.project = repo, project
        self.sets = {}
        self._physical = None
        self._receipt = None
        self._continuation = None
        self._bindings = {}
        self._generic_inputs = {}
        self._head_run, self._head = None, None
        self._charge_run, self._charge_step, self._charges = None, None, {}

    def prepare(self, run_id, *, evaluation=(), generation=()):
        roots = [
            dict(purpose=purpose, ref=ref.model_dump(mode="json"))
            for purpose, refs in (("evaluation", evaluation), ("generation", generation))
            for ref in refs
        ]
        technical = [ref for ref in evaluation if isinstance(ref, RuntimeRef)]
        if technical:
            with _read_connection(self.repo) as tx:
                rows = list(
                    query(
                        tx,
                        """SELECT run_id,input_set_id,archive_ids FROM execution_entries
                    WHERE project_id=:p AND archive_ids && CAST(:ids AS uuid[])""",
                        p=self.project,
                        ids=[ref.record_id for ref in technical],
                    ).mappings()
                )
                private = {str(identity) for row in rows for identity in row["archive_ids"]}
                for row in rows:
                    if row["run_id"] != run_id:
                        from podgen.domain.errors import AccessDenied

                        raise AccessDenied()
                    access.inputs(self.repo, tx, self.project, run_id, row["input_set_id"])
            roots = [root for root in roots if root["ref"].get("record_id") not in private]
        known = self.sets.get(run_id)
        if known and not roots:
            return known
        existing = {digest(root) for root in known["roots"]} if known else set()
        if known and all(digest(root) in existing for root in roots):
            return known
        if not roots and known is None:
            with _read_connection(self.repo) as tx:
                latest = one(
                    tx,
                    "SELECT * FROM execution_input_sets WHERE project_id=:p AND run_id=:r ORDER BY generation DESC LIMIT 1",
                    p=self.project,
                    r=run_id,
                )
                if latest:
                    self.sets[run_id] = dict(latest)
                    self.remember_input_roots(run_id)
                    return self.sets[run_id]
                run = one(
                    tx,
                    "SELECT manifest_id FROM evaluation_writing_runs WHERE project_id=:p AND run_id=:r",
                    p=self.project,
                    r=run_id,
                )
                roots = [
                    dict(
                        purpose="evaluation",
                        ref=RuntimeRef(
                            record_kind="context_assembly", record_id=run["manifest_id"]
                        ).model_dump(mode="json"),
                    )
                ]
        self.sets[run_id] = bind(self.repo, self.project, run_id, roots)
        self.remember_input_roots(run_id)
        return self.sets[run_id]

    def prepare_context(self, run_id, context):
        return self.prepare(run_id, evaluation=[ref for _, _, ref in walk_values(context)])

    def current_head(self, run_id):
        if self._head_run != run_id:
            self._head_run, self._head = run_id, state.head(self.repo, self.project, run_id)
        return self._head

    def committed(self, run_id, head):
        self._head_run, self._head = run_id, head

    def invalidate(self):
        self._head_run, self._head = None, None
        self._charges.clear()
        self._bindings.clear()

    def authorize_inputs(self, run_id, *, action=False):
        from podgen.persistence.execution.reads import authorize_inputs

        return authorize_inputs(self.repo, self, run_id, action=action)

    def publish(self, operation):
        # Compatibility for input-only authorization boundaries. LLM facts use
        # typed Checkpoint exclusively; there is no append/callback write API.
        return _publish(self.repo, self.project, operation)

    def charge_scope(self, run_id, step_id):
        if (self._charge_run, self._charge_step) != (run_id, step_id):
            previous = self.current_head(run_id)
            self._fresh_charge_step = previous is None or previous["active_step_id"] != step_id
            self._charge_run, self._charge_step, self._charges = run_id, step_id, {}

    def charge(self, operation):
        if operation not in self._charges:
            from .checkpoint_accounting import load

            if len(self._charges) >= 4:
                self._charges.clear()
            self._charges[operation] = (0, {}) if self._fresh_charge_step else load(self, operation)
        return self._charges[operation]

    def charges_committed(self, command):
        self.charge_scope(command.run_id, command.step_id)
        for change in command.charges:
            if len(self._charges) >= 4 and change.operation_id not in self._charges:
                self._charges.clear()
            self._charges[change.operation_id] = change.expected_revision + 1, change.new

    def remember_physical(self, run_id, root_id, row, snapshot):
        from .physical_turn import descriptor

        self._physical = run_id, root_id, row["turn_index"], descriptor(row), snapshot

    def current_physical(self, run_id, root_id, index):
        if self._physical and self._physical[:3] == (run_id, root_id, index):
            return self._physical[3:]
        return None

    def reference_owner(self, reference):
        for alias, entry in self._bindings.values():
            if alias == reference:
                return entry
        return None

    def remember_checkpoint(self, command):
        if command.kind not in ("accounting_correction", "legacy_receipt"):
            self._continuation = command.run_id, command.attempt_id, command.entry_id
        for view in command.views:
            payload = view["payload"]
            kind = payload.get("event_kind")
            slot = {
                "count_intent": "count_intent",
                "source_context_count_intent": "count_intent",
                "count_response": "count_receipt",
                "source_context_count": "count_receipt",
                "provider_response": "provider_receipt",
            }.get(kind)
            if payload.get("contract") == "model-request-v1":
                slot = "root_request" if command.step_index is None else "physical_request"
            if slot == "provider_receipt":
                from uuid import UUID

                self._receipt = (
                    command.run_id,
                    command.attempt_id,
                    command.step_index,
                    dict(payload, _record_id=UUID(view["record_id"])),
                )
            if slot:
                self._bindings[slot] = (view["record_id"], command.entry_id)

    def remember_input_roots(self, run_id):
        self._generic_inputs[run_id] = {
            root["ref"]["record_id"] for root in self.sets[run_id]["roots"] if "record_id" in root["ref"]
        }

    def generic_input(self, run_id, record_id):
        return record_id in self._generic_inputs.get(run_id, ())

    def continuation(self, run_id, attempt_id):
        if self._continuation and self._continuation[:2] == (run_id, attempt_id):
            return self._continuation[2]
        with _read_connection(self.repo) as tx:
            row = one(
                tx,
                """SELECT entry_id FROM execution_entries
                WHERE project_id=:p AND run_id=:r AND attempt_id=:attempt
                  AND checkpoint_kind NOT IN ('accounting_correction','legacy_receipt')
                ORDER BY revision DESC LIMIT 1""",
                p=self.project,
                r=run_id,
                attempt=attempt_id,
            )
        return row["entry_id"] if row else None

    def provider_receipt(self, run_id, attempt_id, index):
        if self._receipt and self._receipt[:3] == (run_id, attempt_id, index):
            return self._receipt[3]
        return None
