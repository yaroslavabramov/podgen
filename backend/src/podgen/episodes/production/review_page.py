"""Readable review page for recognition findings; one card per finding with its clip and both texts."""

import html
from pathlib import Path

ROUTES = ("automatic_repair", "user_confirmation", "recognition_variant")
LABELS = (
    "Чужой текст в записи",
    "На подтверждение",
    "Распознаватель записал те же слова иначе",
)
CSS = """
:root{color-scheme:light}
body{background:#f4f5f7;color:#1d1f23;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:980px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:26px;margin:0 0 8px}
h2{font-size:20px;margin:34px 0 10px;padding-top:10px;border-top:2px solid #d9dce1}
.card{background:#fff;border:1px solid #d9dce1;border-left:6px solid #9aa0a6;border-radius:10px;padding:16px 18px;margin:14px 0}
.r0{border-left-color:#d93025;background:#fff8f7}.r1{border-left-color:#f29900}
.meta{display:flex;gap:14px;color:#5f6368;font-size:14px;margin-bottom:8px}.time{font-weight:700;color:#1d1f23}
.id{font-family:ui-monospace,monospace;font-size:12px}
.lbl{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#5f6368;margin-bottom:2px}
.word{font-size:20px;font-weight:600;background:#eef1f5;border-radius:6px;padding:2px 10px;display:inline-block}
.word.heard{background:#fff1c2}.diff{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:10px}
audio{width:100%;margin:6px 0 12px}.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.box{background:#fafbfc;border:1px solid #e6e8eb;border-radius:8px;padding:10px 12px}.box p{margin:4px 0 0}
mark{background:#ffd54f;color:#1d1f23;padding:0 2px;border-radius:3px}mark.gap{background:#ffcdd2;font-style:italic}
@media (max-width:720px){.cols{grid-template-columns:1fr}}
"""


def _time(frames, rate=48000):
    seconds = frames / rate
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _script(candidate, report):
    words = candidate["expected_tokens"]
    if words:
        anchor, end = words[0], words[-1]
        start, stop, gap = anchor["char_start"], end["char_end"], False
    else:
        index = candidate["expected_token_range"][0]
        tokens = report["expected_tokens"]
        anchor = tokens[min(index, len(tokens) - 1)]
        start = stop = anchor["char_start"] if index < len(tokens) else anchor["char_end"]
        gap = True
    text = next(
        u["text"] for u in candidate["expected_context"] if u["utterance_id"] == anchor["utterance_id"]
    )
    left, right = max(0, start - 220), min(len(text), stop + 220)
    middle = (
        '<mark class="gap">⟨в записи здесь лишнее⟩</mark>'
        if gap
        else f"<mark>{html.escape(text[start:stop])}</mark>"
    )
    return (
        ("…" if left else "")
        + html.escape(text[left:start])
        + middle
        + html.escape(text[stop:right])
        + ("…" if right < len(text) else "")
    )


def _heard(candidate, report):
    a, b = candidate["recognized_token_range"]
    tokens = report["recognized_tokens"]
    said = " ".join(t["text"] for t in tokens[a:b]) or "—"
    before = " ".join(t["text"] for t in tokens[max(0, a - 25) : a])
    after = " ".join(t["text"] for t in tokens[b : b + 25])
    return f"…{html.escape(before)} <mark>{html.escape(said)}</mark> {html.escape(after)}…"


def write_page(folder, report, recognition, *, topic, repairs=None):
    clips = {path.stem: path for path in Path(recognition).glob("results/*/*.wav")}
    cards = {route: [] for route in ROUTES}
    for candidate in sorted(report["candidates"], key=lambda c: c["audio_range"]["start_frame"]):
        if candidate["kind"] != "script_discrepancy":
            continue
        rank = ROUTES.index(candidate["review_route"])
        clip = clips.get(candidate["candidate_id"])
        player = (
            f'<audio controls preload="metadata" src="{clip.relative_to(folder).as_posix()}"></audio>'
            if clip
            else "<em>Клипа нет.</em>"
        )
        expected = " ".join(t["text"] for t in candidate["expected_tokens"]) or "—"
        heard = " ".join(t["text"] for t in candidate["recognized_tokens"]) or "—"
        cards[candidate["review_route"]].append(
            f"""<section class="card r{rank}">
<div class="meta"><span class="time">{_time(candidate["audio_range"]["start_frame"])}</span>
<span class="id">находка {candidate["candidate_id"]}</span></div>
<div class="diff"><div><div class="lbl">В сценарии</div><div class="word">{html.escape(expected)}</div></div>
<div><div class="lbl">Распознано в записи</div><div class="word heard">{html.escape(heard)}</div></div></div>
{player}
<div class="cols"><div class="box"><div class="lbl">Текст сценария</div><p>{_script(candidate, report)}</p></div>
<div class="box"><div class="lbl">Что прозвучало</div><p>{_heard(candidate, report)}</p></div></div>
</section>"""
        )
    sections = "".join(
        f"<h2>{label} — {len(cards[route])}</h2>" + "".join(cards[route])
        for route, label in zip(ROUTES, LABELS, strict=True)
        if cards[route]
    )
    done = ""
    if repairs:
        done = "<h2>Уже заменено</h2>" + "".join(
            f'<section class="card"><p>{html.escape(item["text"])}</p></section>'
            for item in repairs["proposals"]
        )
    page = f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="color-scheme" content="light">
<title>{html.escape(topic)} — проверка звука</title><style>{CSS}</style>
<h1>{html.escape(topic)}</h1>
<p>Найдено {sum(map(len, cards.values()))} расхождений записи со сценарием. Чтобы заменить фразу, передайте номер находки в
<code>episode_cli repair --candidate-id</code>; когда всё в порядке — <code>episode_cli accept</code>.</p>
{done}{sections}</html>"""
    (Path(folder) / "index.html").write_text(page, encoding="utf-8")
