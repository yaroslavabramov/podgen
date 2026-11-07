"""Complete archived 83 request and real independently validated reviews, with no model calls."""

import json
from pathlib import Path


def actual_case():
    root = Path(__file__).parent / "fixtures"
    request = json.loads((root / "revision-83.request.json").read_text())
    payload = json.loads(request["input"][0]["content"][0]["text"])
    script = dict(
        utterances=[{k: v for k, v in row.items() if k != "index"} for row in payload["script"]["utterances"]]
    )
    reviews = {
        origin: json.loads((root / f"revision-83.{origin}.json").read_text()) for origin in ("reader", "fact")
    }
    return request, payload, script, reviews


def scope(scale="local", anchors=(), **extra):
    return dict(
        scale=scale,
        reason="Explicit editorial test decision.",
        anchors=list(anchors),
        dependent_passages=[],
        **extra,
    )


def reviews_for(script, quote, index=0):
    anchor = dict(utterance_index=index, quote=quote)
    return dict(
        reader=dict(recommendation="keep", revision_scope=scope("none"), findings=[]),
        fact=dict(
            recommendation="revise",
            revision_scope=scope(anchors=[anchor]),
            findings=[dict(anchors=[anchor], priority="important")],
        ),
    )


def composition(label="New"):
    return dict(
        main_question=label + " question",
        listener_promise=label + " promise",
        route_summary=label + " route",
        decisions=[label + " order and reason"],
    )


def corrections_83():
    return [
        dict(
            utterance_index=0,
            quote="Восемьдесят три человека погибли за последние три дня той недели.",
            text="Сноу подробно расследует восемьдесят три смерти, зарегистрированные за эту неделю в трёх подрайонах как произошедшие в последние три дня.",
        ),
        dict(
            utterance_index=4,
            quote="Особенно важны десять адресов в стороне.",
            text="Особенно важны десять случаев смерти в стороне.",
        ),
        dict(
            utterance_index=11,
            quote="там заболели люди, месяцами не бывавшие на Брод-стрит",
            text="там заболели женщина, месяцами не бывавшая на Брод-стрит, и её племянница",
        ),
    ]
