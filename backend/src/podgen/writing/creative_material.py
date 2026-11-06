"""Full factual material for storytelling, separate from editorial checks and identities.

Callers select cards and attach their routing IDs. Stored cards and checker views
retain titles, labels, limitations, excerpts and dossier-wide research context.
"""


def creative_card_view(card, *, sources):
    """Use the authored account as a whole; old inputs retain their full factual context."""
    result = dict(sources=list(dict.fromkeys(sources)))
    story_facts = getattr(card, "story_facts", None)
    if story_facts is not None:
        result["claim"] = story_facts
    else:
        result.update(claim=card.claim, context=card.context)
    return result


def creative_dossier_view(cards):
    """Accept already selected/identified card views; no preselected summary or warnings."""
    return dict(knowledge_cards=list(cards))
