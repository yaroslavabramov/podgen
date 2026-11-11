"""Human-readable independent reading results, including refusals and every exact script version."""

from pathlib import Path

from podgen.domain.contracts.common import RuntimeRef

from .material_names import display_materials, frozen_names
from .reading_outcome import read_outcome
from .reading_workflow import base_stage


def spoken_text(script):
    return "\n\n".join(row["speaker"] + ": " + row["text"] for row in script["utterances"]) + "\n"


def export_readings(journal, run_id, manifest, write_json, write_text):
    outcome = read_outcome(journal, run_id)
    names = frozen_names(journal, run_id, manifest)
    ru = manifest["output_language"].lower().startswith("ru")
    status = outcome["status"] if outcome else "incomplete"
    messages = {
        "keep": (
            "Оба чтения завершены: обязательных правок нет.",
            "Both readings completed: no required changes.",
        ),
        "needs_material": (
            "Нужны дополнительные материалы. Финал не одобрен.",
            "More material is required. The final script was not kept.",
        ),
        "revision_limit_exhausted": (
            "Лимит правок исчерпан. В финале остались замечания.",
            "Revision limit exhausted. Findings remain in the final candidate.",
        ),
        "incomplete": (
            "Проверка не завершена. См. сохранённые попытки и причину остановки.",
            "Review is incomplete. See the saved attempts and failure reason.",
        ),
    }
    lines = [
        "# " + ("Результат двух чтений" if ru else "Independent reading result"),
        "",
        messages[status][0 if ru else 1],
        "",
    ]
    initial = manifest["command"].get("initial_script_ref")
    if initial:
        script = journal.read(RuntimeRef.model_validate(initial))
        write_json(Path("draft.json"), script)
        write_text("draft.txt", spoken_text(script["output"]))
    for stage in manifest["stages"]:
        if base_stage(stage) == "revise":
            invocation = journal.invocation(run_id, stage)
            if invocation:
                request = journal.read(
                    RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
                )
                if "revision_operation" in request["context"]:
                    write_json(Path(stage + "-revision-context.json"), request["context"])
            if invocation and invocation["status"] == "completed":
                version = journal.output(invocation)
                if "revision" in version:
                    write_json(Path(stage + "-application.json"), version["revision"])
                    write_json(Path(stage + "-composition.json"), version["composition"])
        if base_stage(stage) not in ("reader-review", "fact-review"):
            continue
        invocation = journal.invocation(run_id, stage)
        if not invocation or invocation["status"] != "completed":
            continue
        review = journal.output(invocation)["output"]
        index = int(stage.rsplit("-", 1)[1]) if stage[-1].isdigit() else 0
        title = (
            ("Чтение слушателя" if base_stage(stage) == "reader-review" else "Фактологическая проверка")
            if ru
            else ("Listener reading" if base_stage(stage) == "reader-review" else "Fact review")
        )
        if "assignment_results" in review:
            title = "Редакторская перечитка" if ru else "Review of the revision"
        from .editorial_continuation.source import offset

        title += (" · версия " if ru else " · version ") + str(offset(manifest) + index + 1)
        lines.extend(["## " + title, "", review["recommendation"], "", review["listener_answer"], ""])
        if "assignment_results" in review:
            from .recheck_source import load

            _, comparison = load(journal, run_id, stage, version=manifest["revision_verification_contract"])
            orders = {row["id"]: row for row in comparison["assignments"]}
            lines.extend(
                [
                    "### " + ("Что изменила перепись" if ru else "Effect of the revision"),
                    "",
                    review["effect_on_episode"],
                    "",
                ]
            )
            for result in review["assignment_results"]:
                order = orders[result["assignment_id"]]
                assignment = (
                    order["action"]
                    if "action" in order
                    else order["requested_change"]
                    if "requested_change" in order
                    else comparison["scopes"][order["scope"]]["reason"]
                )
                lines.extend(
                    [f"**{order['id']} · {result['outcome']}**", "", assignment, "", result["judgment"], ""]
                )
        if "revision_scope" in review:
            scope = review["revision_scope"]
            lines.extend(
                [
                    "### " + ("Масштаб правки" if ru else "Repair scale"),
                    "",
                    scope["scale"],
                    "",
                    scope["reason"],
                    "",
                ]
            )
            for anchor in scope["anchors"]:
                lines.extend([f"{anchor['utterance_index']}: {anchor['quote']}", ""])
        for strength in review["strengths"]:
            lines.extend(["### " + ("Что работает" if ru else "What works"), "", strength["reason"], ""])
            for anchor in strength["anchors"]:
                lines.extend([f"{anchor['utterance_index']}: {anchor['quote']}", ""])
        for finding in review["findings"]:
            lines.extend(["### " + finding["priority"] + " / " + finding["dimension"], ""])
            for anchor in finding["anchors"]:
                lines.extend([f"{anchor['utterance_index']}: {anchor['quote']}", ""])
            lines.extend([finding["observation"], "", finding["requested_change"], ""])
            lines.extend(text for item in finding["preserve"] for text in (item, ""))
            if finding.get("support_labels"):
                lines.extend([display_materials(finding["support_labels"], names), ""])
        for need in review["research_requests"]:
            lines.extend([need["question"], "", need["why_needed"], ""])
            for anchor in need["anchors"]:
                lines.extend([f"{anchor['utterance_index']}: {anchor['quote']}", ""])
        lines.extend(text for item in review["important_limits"] for text in (item, ""))
        lines.extend([review["duration_note"], ""])
    if outcome:
        write_json(Path("review-outcome.json"), outcome)
        for index, ref in enumerate(outcome["rounds"]):
            record = journal.read(RuntimeRef.model_validate(ref))
            write_json(Path(f"reading-round-{index}.json"), record)
        name = "final" if status == "keep" else "candidate-final"
        write_json(Path(name + ".json"), outcome)
        write_text(name + ".txt", spoken_text(outcome["output"]))
    lines.append(
        "Проверка моделей не является допуском к публикации."
        if ru
        else "Model readings do not grant production approval."
    )
    write_text("review-result.md", "\n".join(lines) + "\n")
    return dict(review_status=status, final_script_ref=outcome["script_ref"] if outcome else None)
