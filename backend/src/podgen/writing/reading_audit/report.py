"""Human-readable audit decisions; counts are coverage bookkeeping, never a truth score."""

from .validation import decisions


def render(review):
    audit = review["audit"]
    lines = ["", "### Audit", "", audit["overall_judgment"], ""]
    if review["effect_on_episode"] is not None:
        lines.extend(["### Effect of the revision", "", review["effect_on_episode"], ""])
    for result in review["assignment_results"]:
        lines.extend([f"**{result['assignment_id']} · {result['outcome']}**", "", result["judgment"], ""])
        if result["followup_finding"] is not None:
            lines.extend([f"Follow-up finding: {result['followup_finding']}", ""])
    for area in audit["areas"]:
        lines.extend([f"**{area['aspect']} · {area['status']}**", "", area["explanation"], ""])
        if area["decision_ids"]:
            lines.extend(["Decisions: " + ", ".join(area["decision_ids"]), ""])
    for block in audit["coverage"]:
        lines.extend(
            [
                f"{block['first_utterance_index']}–{block['last_utterance_index']} · {block['status']}",
                "",
                block["explanation"],
                "",
            ]
        )
    for item in decisions(review):
        for anchor in item["anchors"]:
            lines.extend([f"{anchor['utterance_index']}: {anchor['quote']}", ""])
        if item.get("source_refs"):
            lines.extend(
                [
                    "Evidence: "
                    + ", ".join(f"{ref['from_review']}:{ref['evidence_id']}" for ref in item["source_refs"]),
                    "",
                ]
            )
        lines.extend(
            [
                f"**{item['decision_id']} · {item['verdict']}**",
                "",
                item["contextual_meaning"],
                "",
                item["context_scope"],
                "",
                item["rationale"],
                "",
            ]
        )
    for item in review["previous_dispositions"]:
        lines.extend(["Current decisions: " + ", ".join(item["current_decision_ids"]), ""])
        lines.extend(
            [
                f"**{item['previous_decision_id']} · {item['disposition']}**",
                "",
                item["context_assessment"],
                "",
                item["reason"],
                "",
            ]
        )
    for item in review["late_findings"]:
        lines.extend(
            [
                "Previous decisions: " + ", ".join(item["previous_decision_ids"]),
                "Previous coverage: " + ", ".join(map(str, item["previous_coverage_indices"])),
                "Assignments: " + ", ".join(item["assignment_ids"]),
                "Current decisions: " + ", ".join(item["current_decision_ids"]),
                "",
            ]
        )
        lines.extend(
            [
                f"**{item['finding_index']} · {item['reason_kind']}**",
                "",
                item["grounds"],
                "",
                item["substantive_consequence"],
                "",
            ]
        )
    return lines
