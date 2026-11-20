"""Durable, run-scoped document acquisition inside an unfinished editorial check."""

import asyncio
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.research.hosted.corpus_acquisition import CorpusAcquisition, document_url
from podgen.research.hosted.journal import ResearchJournal
from podgen.sources.acquisition.contracts import AcquisitionRequest
from podgen.sources.acquisition.service import acquire_source
from podgen.sources.material_requests import MaterialResolution

from .admissions import admit_outcome, entries, load_admission


def same_document(reported_url, canonical):
    try:
        return document_url(reported_url) == canonical
    except (ValueError, ContractError):
        return False


class ResearchMaterialResolver:
    def __init__(self, journal, materials, *, acquire=acquire_source, parent_run_ids=()):
        self.journal, self.materials, self.acquire = journal, materials, acquire
        self.parent_run_ids = tuple(parent_run_ids)
        self.preparation = journal.repo.get_runtime(journal.project, materials.shared_ref)["preparation"]
        config = self.preparation.get("acquisition")
        self.config = CorpusAcquisition.model_validate(config) if config else None

    def store(self, run_id):
        return ResearchJournal(self.journal.repo, self.journal.project, run_id)

    def restore(self, *, run_id, guard):
        store = self.store(run_id)
        from .inheritance import freeze

        # Freeze may publish inheritance. Finish it before the read-only batch.
        guard()
        freeze(self.journal, store, self.materials, lambda: None, self.parent_run_ids)
        from podgen.persistence.read_authorization import read_operation

        with read_operation(self.journal.repo):
            admissions, references = [], [self.materials.shared_ref]
            for row in entries(store):
                if row["shared_input_ref"] != self.materials.shared_ref.model_dump(mode="json"):
                    raise ContractError("research_added_source_input_mismatch")
                admissions.append(load_admission(store, row))
                from podgen.domain.contracts.common import RuntimeRef

                references.append(RuntimeRef.model_validate(row["_admission_ref"]))
            self.journal.repo.authorize_runtime_many(self.journal.project, references)
            return tuple(admissions)

    def resolve(self, name, arguments, *, operation_id, run_id, guard):
        guard()
        if self.config is None:
            return MaterialResolution(dict(error="source_acquisition_not_configured"))
        store = self.store(run_id)
        from .inheritance import freeze

        freeze(self.journal, store, self.materials, guard, self.parent_run_ids)
        with store.lock():
            key = "material-request:" + str(operation_id)
            store.put(
                key + ":request",
                dict(
                    name=name,
                    arguments=arguments,
                    shared_input_ref=self.materials.shared_ref.model_dump(mode="json"),
                ),
            )
            from .orders import KEY
            from .targeted_reparse import cached_result, save_result

            if name == "request_research" and KEY in arguments:
                from .order_resolution import resolve

                return resolve(self, store, key, arguments, guard)
            result_key, saved = cached_result(store, key, name, arguments)
            if saved:
                return MaterialResolution(saved, self.restore(run_id=run_id, guard=guard))
            if result_key != key + ":result":
                from .targeted_reparse import resolve_saved

                resolution = resolve_saved(self, store, key, arguments, guard)
            elif name == "request_research":
                from .research_requests import resolve_research

                resolution = resolve_research(self, store, key, arguments, operation_id, guard)
            elif name == "request_source":
                source_id, url = arguments.get("source_id"), arguments.get("url")
                if bool(source_id) == bool(url):
                    return MaterialResolution(dict(error="provide_exactly_one_source_id_or_url"))
                if source_id:
                    known = next((s for s in self.materials.packet.sources if s.source_id == source_id), None)
                    added = next(
                        (row for row in entries(store) if row["source"]["source_id"] == source_id), None
                    )
                    url = known.reported_url if known else added["url"] if added else None
                    if url is None:
                        return MaterialResolution(dict(error="unknown_source_id"))
                resolution = self.document(store, url, guard)
                if source_id:
                    resolution = MaterialResolution(
                        dict(resolution.result, requested_source_id=source_id), resolution.admissions
                    )
            else:
                return MaterialResolution(dict(error="unknown_material_request"))
            if resolution.result.get("status") != "pending":
                save_result(store, key, result_key, resolution.result)
            return resolution

    def document(self, store, url, guard):
        try:
            canonical = document_url(url)
        except (ValueError, ContractError):
            return MaterialResolution(dict(error="invalid_source_url"))
        for source in self.materials.packet.sources:
            if source.document and same_document(source.reported_url, canonical):
                return MaterialResolution(
                    dict(
                        status="resolved",
                        source_id=source.source_id,
                        title=source.title,
                        availability=source.status,
                        reused=True,
                    )
                )
        for row in entries(store):
            if row["url"] == canonical:
                admission = load_admission(store, row)
                return MaterialResolution(
                    dict(
                        status="resolved", source_id=admission.source_id, title=admission.title, reused=True
                    ),
                    (admission,),
                )
        scope = digest(
            dict(
                url=canonical,
                profile=self.config.profile_ref.model_dump(mode="json"),
                rights=self.config.rights.model_dump(mode="json"),
            )
        )
        key = "material-document:" + scope
        outcome = store.get(key)
        if outcome is None:
            guard()
            request = AcquisitionRequest(
                operation_id=uuid5(store.run_id, key),
                source_id=uuid5(store.project, "reported-source:" + canonical),
                url=canonical,
                profile_ref=self.config.profile_ref,
                rights=self.config.rights,
            )
            outcome = asyncio.run(self.acquire(store.repo, store.project, request)).model_dump(mode="json")
            store.put(key, outcome)
        guard()
        admission = admit_outcome(store, self.materials, canonical, outcome)
        if admission is None:
            return MaterialResolution(
                dict(
                    status="unavailable",
                    url=canonical,
                    attempt_ref=store.ref(key).model_dump(mode="json"),
                    diagnostics=outcome["diagnostics"],
                    representations=outcome["sources"],
                )
            )
        return MaterialResolution(
            dict(
                status="resolved",
                source_id=admission.source_id,
                title=admission.title,
                availability=admission.catalog["status"],
                admission_ref=admission.catalog["admission_ref"],
            ),
            (admission,),
        )
