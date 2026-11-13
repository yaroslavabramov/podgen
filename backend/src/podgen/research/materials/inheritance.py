"""Freeze original admission references when a writing run reuses earlier work."""

from uuid import UUID

from podgen.domain.errors import ContractError
from podgen.research.hosted.journal import ResearchJournal

from .admissions import entries


def freeze(journal, store, materials, guard, parent_run_ids=()):
    if store.get("material-inheritance") is not None:
        return
    parents = list(parent_run_ids)
    fixed_parent = getattr(materials.fixed, "source_run_id", None)
    if fixed_parent is not None and fixed_parent != store.run_id:
        parents.append(fixed_parent)
    if journal.run(store.run_id) is not None:
        manifest = journal.manifest(store.run_id)
        for key in ("planning_continuation", "editorial_continuation"):
            if manifest.get(key):
                parents.append(UUID(manifest[key]["parent_run_id"]))
    parents = list(dict.fromkeys(parents))
    if store.run_id in parents:
        raise ContractError("research_material_inheritance_cycle")
    with store.lock():
        if store.get("material-inheritance") is not None:
            return
        rows = {}
        for parent in parents:
            guard()
            source = ResearchJournal(store.repo, store.project, parent)
            for row in entries(source):
                guard()
                if row["shared_input_ref"] != materials.shared_ref.model_dump(mode="json"):
                    raise ContractError("research_inherited_source_input_mismatch")
                source_id = row["source"]["source_id"]
                ref = row["_admission_ref"]
                if source_id in rows and rows[source_id] != ref:
                    raise ContractError("research_inherited_source_id_collision")
                rows[source_id] = ref
        store.put(
            "material-inheritance",
            dict(parent_run_ids=[str(parent) for parent in parents], admission_refs=list(rows.values())),
        )
