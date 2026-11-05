"""Atomic technical import of the real WP-02 file package, with no fabricated semantic mappings."""

import hashlib
import json
import mimetypes
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.editorial_staging import StagedEditorialFile, staged_spoken_view
from podgen.domain.errors import AccessDenied, Conflict, ContractError

from .runtime_records import append_runtime

STAGING_IMPORTER_VERSION = "1.1"
CASE_SOURCES = {"gps-clock": "gps", "snow-pump": "snow"}
MAPPING = [
    "EpisodeSpec: authorized policies, voice/profile refs, confirmed temporal and source scope",
    "Sources: original acquisition runtime refs, rights policies, extraction profiles and original anchors",
    "ResearchNote: local G/S citations -> exact EvidenceSpan refs, interpretation ranges and review",
    "DossierSnapshot: canonical selected-note/source/evidence closure and independent acceptance",
    "StoryPlan: exact dossier/spec/commission refs, options/acts/speaker IDs and host functions",
    "ScriptRevision: canonical stable act/block/item/speaker IDs, exact plan/dossier refs and change identity mapping",
    "EvaluationAnnotation: explicit schema fields, source refs, evaluation policy and isolated evaluator ACL",
]


def snapshot(root):
    root = Path(root).resolve()
    result = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ContractError("staging_symlink_not_allowed")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    if "inventory.sha256.json" not in result:
        raise ContractError("staging_inventory_missing")

    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ContractError("staging_inventory_duplicate_path")
            values[key] = value
        return values

    inventory = json.loads(result["inventory.sha256.json"].decode("utf-8"), object_pairs_hook=unique)
    actual = {
        path: hashlib.sha256(data).hexdigest()
        for path, data in result.items()
        if path != "inventory.sha256.json"
    }
    if not isinstance(inventory, dict) or inventory != actual:
        raise ContractError(
            "staging_inventory_mismatch", "Frozen paths and all file hashes must match before import."
        )
    for case, source in CASE_SOURCES.items():
        for relative in [
            f"author/{case}/brief.md",
            f"author/{case}/dossier.md",
            f"author/{case}/plan.md",
            f"evaluator-only/{case}/draft.txt",
            f"evaluator-only/{case}/final.txt",
            f"sources/{source}/evidence.json",
        ]:
            if relative not in result:
                raise ContractError("staging_required_file_missing", relative)
    if "PROVENANCE.md" not in result:
        raise ContractError("staging_provenance_missing")
    return result


def author_case(path):
    for case, source in CASE_SOURCES.items():
        if path.startswith(f"author/{case}/") or path.startswith(f"sources/{source}/"):
            return case
    return None


def original_author(path):
    if path.startswith("sources/"):
        if Path(path).suffix == ".md":
            return "model"
        if path.endswith(("evidence.json", ".acquisition.json")):
            return "mixed_package_metadata"
        return "external_source"
    if Path(path).suffix in (".md", ".txt", ".py"):
        return "model"
    return "mixed_package_metadata"


class EditorialStaging:
    def __init__(self, repository):
        self.repo = repository

    def import_directory(self, project, root):
        repo = self.repo
        with repo.database.transaction() as tx:
            if repo.purpose != "evaluation" or repo._acl(tx, project, write=True) not in (
                "owner",
                "evaluator",
                "operator",
            ):
                raise AccessDenied()
        files = snapshot(root)
        hashes = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
        package_hash = digest(hashes)
        package_id = uuid5(
            NAMESPACE_URL, f"podgen-editorial-staging-import-{STAGING_IMPORTER_VERSION}:" + package_hash
        )
        refs = {
            path: RuntimeRef(record_kind="user_input", record_id=uuid5(package_id, path)) for path in files
        }
        blobs = {}
        import io

        for path, data in files.items():
            media = mimetypes.guess_type(path)[0] or "application/octet-stream"
            blobs[path] = repo.save_blob(
                project, io.BytesIO(data), media, expected_sha256=hashes[path], expected_length=len(data)
            )
        # No partial runtime publication if an active editorial rewrite changes the captured package.
        if {p: hashlib.sha256(data).hexdigest() for p, data in snapshot(root).items()} != hashes:
            raise Conflict("editorial_package_changed_during_import")
        author_inputs = {case: [] for case in CASE_SOURCES}
        script_views = {}
        with repo.database.transaction() as tx:
            repo._acl(tx, project, write=True)
            repo._lock(tx, project)
            for path, data in files.items():
                case = author_case(path)
                audience = "project" if case else "evaluator"
                if case:
                    author_inputs[case].append(refs[path])
                text = (
                    data.decode("utf-8")
                    if Path(path).suffix in (".md", ".txt", ".json", ".py", ".html")
                    else None
                )
                script = path.startswith("evaluator-only/") and path.endswith(("/draft.txt", "/final.txt"))
                view = staged_spoken_view(text) if script else []
                if script:
                    script_views[path] = [item.model_dump(mode="json") for item in view]
                prior = (
                    refs.get(path.removesuffix("final.txt") + "draft.txt")
                    if path.endswith("/final.txt")
                    else None
                )
                kind = (
                    ("script_final" if path.endswith("/final.txt") else "script_draft")
                    if script
                    else (
                        "author_input"
                        if path.startswith("author/")
                        else "source_material"
                        if path.startswith("sources/")
                        else "provenance"
                        if path == "PROVENANCE.md"
                        else "evaluator_material"
                    )
                )
                record = StagedEditorialFile(
                    contract="editorial-staging-v1",
                    source_path=path,
                    material_kind=kind,
                    technical_fixture=True,
                    semantic_acceptance=False,
                    original_blob=blobs[path],
                    full_text=text,
                    staged_spoken_view=view,
                    prior_staged_script_ref=prior,
                    original_author_kind=original_author(path),
                    exact_model_id=None,
                    model_invocation_archive=None,
                    independent_approvals=[],
                    mapping_status="unmapped",
                    remaining_mapping=MAPPING,
                )
                append_runtime(
                    repo, tx, project, refs[path], record.model_dump(mode="json"), audience, replay=True
                )
            author_refs = {}
            for case, allowed in author_inputs.items():
                ref = RuntimeRef(record_kind="user_input", record_id=uuid5(package_id, "author:" + case))
                append_runtime(
                    repo,
                    tx,
                    project,
                    ref,
                    dict(
                        contract="editorial-staging-author-v1",
                        case_id=case,
                        technical_fixture=True,
                        semantic_acceptance=False,
                        input_refs=[r.model_dump(mode="json") for r in allowed],
                    ),
                    replay=True,
                )
                author_refs[case] = ref
            evaluator = RuntimeRef(record_kind="user_input", record_id=uuid5(package_id, "evaluator"))
            append_runtime(
                repo,
                tx,
                project,
                evaluator,
                dict(
                    contract="editorial-staging-evaluator-v1",
                    package_hash=package_hash,
                    files={p: r.model_dump(mode="json") for p, r in refs.items()},
                ),
                audience="evaluator",
                replay=True,
            )
        # Full round-trip compares current snapshot bytes/text/utterance order, never old counts/hashes.
        for path, ref in refs.items():
            saved = repo.get_runtime(project, ref)
            if saved["full_text"] is not None and saved["full_text"].encode("utf-8") != files[path]:
                raise ContractError("staging_text_roundtrip_failed", path)
            if saved["staged_spoken_view"] != script_views.get(path, []):
                raise ContractError("staging_spoken_order_roundtrip_failed", path)
            if repo.read_runtime_blob(project, ref, blobs[path]) != files[path]:
                raise ContractError("staging_blob_roundtrip_failed", path)
        return dict(
            contract="editorial-staging-report-v1",
            importer_version=STAGING_IMPORTER_VERSION,
            technical_fixture=True,
            semantic_acceptance=False,
            production_ready=False,
            package_id=str(package_id),
            package_hash=package_hash,
            file_hashes=hashes,
            author_inputs={case: ref.model_dump(mode="json") for case, ref in author_refs.items()},
            evaluator_index=evaluator.model_dump(mode="json"),
            script_views=script_views,
            file_refs={path: ref.model_dump(mode="json") for path, ref in refs.items()},
            remaining_mapping=MAPPING,
            provenance=dict(
                author="model; see the full persisted PROVENANCE.md",
                exact_model_id=None,
                model_invocation_archive=None,
                independent_approvals=[],
            ),
        )
