"""V6 planning field tasks; persisted contracts and earlier schema text stay unchanged."""

STORY_DESCRIPTIONS = {
    "PlanProposal": {
        "listener_promise": "What following the chosen story offers this audience.",
        "working_answer": "The chosen answer to the episode's question, with the context needed to understand it.",
        "route_summary": "The chosen story from opening to ending: what happens or becomes clear, how its developments connect, and where they lead.",
    },
    "PlanningOpening": {
        "approach": "What listeners encounter at the start, what they learn or notice next, and how this opens the chosen story.",
        "question_to_open": "The question raised for listeners by this opening.",
    },
    "PlanningAct": {
        "title": "A short name for this part of the chosen story.",
        "listener_question": "What listeners want to find out at this point in the story.",
        "intended_change_in_understanding": "What happens, is discovered, or becomes clear in this part.",
        "prerequisite_concepts": "Technical knowledge needed to follow this part; an empty list is valid.",
    },
    "PlanningTransitions": {
        "from_previous": "How the preceding development leads into this part.",
        "toward_next": "How this part leads to the next development or question.",
    },
    "PlanningEnding": {
        "answer_to_deliver": "The answer reached at the ending.",
    },
    "PlanningHostFunction": {
        "contribution": "What this person tells, explains, notices, asks, or contributes to this particular story.",
        "interaction_guidance": "How this narrator carries the account forward, or how the speakers' contributions build on each other.",
        "speech_register": "How this person speaks for this audience and genre.",
    },
    "PlanningDurationStrategy": {
        "assumptions": "The timing basis for the duration estimate, such as pace, pauses, and how the parts are counted.",
        "optional_material": "Specific story material that can be added or removed to fit the duration; an empty list is valid.",
    },
}


def describe_selected_story(schema):
    for name, fields in STORY_DESCRIPTIONS.items():
        for field, description in fields.items():
            schema["$defs"][name]["properties"][field]["description"] = description
