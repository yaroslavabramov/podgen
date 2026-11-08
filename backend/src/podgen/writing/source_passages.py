"""Deliver only explicitly selected, exact, attributed narrative source passages."""


def card_source_passages(card, documents):
    passages = []
    seen = set()
    for index in getattr(card, "narrative_excerpt_indices", None) or []:
        excerpt = card.excerpts[index]
        identity = (excerpt.document, excerpt.start, excerpt.end)
        if identity in seen:
            continue
        seen.add(identity)
        passages.append(dict(source=documents[excerpt.document].title, text=excerpt.text))
    return passages
