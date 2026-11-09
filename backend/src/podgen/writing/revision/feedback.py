"""Historical full-review author views, kept byte-reproducible for existing run pins."""

from ..projections import actionable_feedback, evidence_view


def guidance(readings, *, projection_version, addressed):
    result = [
        dict(origin=origin, **actionable_feedback(readings[origin], projection_version=projection_version))
        for origin in ("reader", "fact")
    ]
    if addressed:
        for review in result:
            if review["origin"] == "fact":
                for field in ("listener_understanding", "duration_note", "important_limits"):
                    review.pop(field, None)
    return result


def attach_evidence(payload, packet, needed_labels):
    if packet.contract == "editorial-input/2":
        passages = evidence_view(packet)
        for review in payload["revision_guidance"]:
            if review["origin"] != "fact":
                continue
            for finding in review["findings"]:
                references = set(finding.pop("support_labels"))
                finding["evidence"] = [
                    dict(source=passage["source"], text=passage["text"])
                    for passage in passages
                    if references.intersection(passage["supports"])
                ]
    elif needed_labels:
        payload["repair_evidence"] = [
            row for row in evidence_view(packet) if needed_labels.intersection(row["supports"])
        ]
