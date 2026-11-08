"""Readable scene-development plan and its clearly separate internal notes."""


def render_scenes(plan, notes, lines, labels, section, field, material_names, seconds):
    section("opening")
    field("development", plan["opening"]["development"])
    field("refs", material_names(plan["opening"]["material_refs"]))
    section("acts")
    for index, act in enumerate(plan["acts"], 1):
        lines.extend(["", f"### {index}. {act['title']}"])
        field("development", act["development"])
        field("refs", material_names(act["material_refs"]))
        field("time", seconds(act["estimated_seconds_range"]) + "; " + labels[act["priority"]])
    section("ending")
    field("development", plan["ending"]["development"])
    field("refs", material_names(plan["ending"]["material_refs"]))
    if notes:
        section("editorial_notes")
        titles = {
            "opening": labels["opening"],
            "ending": labels["ending"],
            **{act["act_id"]: act["title"] for act in plan["acts"]},
        }
        for row in notes:
            lines.append("**" + titles[row["scene"]] + "**")
            field(row["kind"], row["note"])
            field("refs", material_names(row["material_refs"]))
