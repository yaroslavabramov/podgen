"""Planning ending intent for story-resolution/2; published shapes and /1 descriptions stay intact."""


def describe(schema, stage):
    if stage in ("plan", "plan-revise"):
        definitions = schema["$defs"]
        ending = definitions["PlanningFinalMoment"]["properties"]
        ending["last_moment"]["description"] = (
            "The intent and content of the concluding speech about the whole selected story: what "
            "the speaker will say about the events or explanation in closing. An ordinary summary "
            "can be appropriate; the speaker's attitude may shape it. Include qualifications when "
            "the retained meaning needs them. Give the writer a developed intent, not composed "
            "spoken lines. The concluding words may belong to the final scene or a separate closing "
            "passage; an added afterword is not required. No particular emotion, new fact or final turn is required."
        )
        ending["why_end_here"]["description"] = (
            "How the concluding speech relates to the whole chosen story and completes the episode "
            "for this audience, within the final scene or in a separate closing passage. Explain the choice of content and "
            "perspective; a summary may close the account without a new discovery or emotion."
        )
        proposal = definitions["PlanProposalV9"]["properties"]
        proposal["working_answer"]["description"] = (
            "The supported answer or meaning established across the whole episode, including "
            "uncertainty that changes it. This is planning meaning, not ready-made spoken lines; "
            "ending selects what the concluding speech will say about the chosen story."
        )
        proposal["route_summary"]["description"] = (
            "The selected story from its prepared opening through connected developments and "
            "the final act, including the intended closing words about the whole story. These may belong "
            "to the final scene or a separate closing passage. Select or combine branches that repeat the "
            "same contribution; keep event chronology and the timing of discoveries accurate."
        )
    elif stage.startswith("plan-review"):
        props = schema["properties"]
        props["listener_answer"]["description"] = (
            "Follow the selected story as a newcomer: the opening premises, developments, first "
            "disclosures and chronology of actions and knowledge. Assess the concluding speech "
            "planned in ending against the whole chosen story and the final act: what it will say "
            "in closing and how it completes this account, within the final scene or in a separate "
            "closing passage. An ordinary summary or the speaker's "
            "attitude may be appropriate; no particular emotion, new discovery or final turn is required."
        )
        props["findings"]["description"] = (
            "Addressed defects in the selected story, novice prerequisites, opening promise, "
            "chronological implications, overlapping branches or intended concluding speech. "
            "Judge the ending choice by the whole story it closes, including whether its summary, "
            "perspective and necessary qualifications fit the retained account. Require a repair "
            "for a concrete defect, not for lacking a preferred closing technique. Empty is valid "
            "when the plan works."
        )
