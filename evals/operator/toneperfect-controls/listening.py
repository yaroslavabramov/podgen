"""Render a local listening report from saved assessment evidence; no API calls."""

import argparse
import hashlib
import html
import json
from pathlib import Path

LABELS = {
    "review": "Требует проверки",
    "uncertain": "Не уверен",
    "no_correction": "Не предлагает исправление",
    "partial": "Не удалось полностью оценить",
    "unassessable": "Не удалось оценить",
    "content_mismatch_suspected": "Подозревает другой текст",
    None: "Нет решения",
}


def render(data_path: Path, output_dir: Path):
    data = json.loads(data_path.read_text())
    output_dir.mkdir(parents=True, exist_ok=True)
    cards = []
    for index, case in enumerate(data["cases"], 1):
        audio = Path(case["audio_path"]).read_bytes()
        if hashlib.sha256(audio).hexdigest() != case["audio_sha256"]:
            raise ValueError("audio_hash_mismatch")
        filename = f"clip-{index:02d}.wav"
        (output_dir / filename).write_bytes(audio)
        reference = html.escape(case["reference_text"])
        rows = []
        for word in case["target_rows"]:
            decision = word.get("decision")
            label = LABELS.get(decision, f"Другое решение: {decision}")
            text = word.get("word", word.get("text", ""))
            rows.append(
                f"<tr><td>{html.escape(text)}</td><td>{html.escape(label)}</td>"
                f"<td>{html.escape(str(word.get('score')))}</td></tr>"
            )
        nearby = case.get("other_flagged_words", [])
        extra = ", ".join(
            f"{word.get('word', word.get('text', ''))}: "
            f"{LABELS.get(word.get('decision'), str(word.get('decision')))}"
            for word in nearby
        )
        clock = html.escape(case["timestamp"])
        confirmation = html.escape(case["human_status"])
        cards.append(
            f'<article id="clip-{index:02d}"><h2>{index:02d} · {clock}</h2>'
            f'<audio controls preload="none" src="{filename}"></audio>'
            f"<details><summary>Сценарий и ответ сервиса</summary><p>{reference}</p>"
            "<table><thead><tr><th>Проверяемое слово</th><th>Решение сервиса</th>"
            "<th>Балл</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
            f"<p>Подтверждение на слух: {confirmation}</p>"
            + (
                f"<p>Другие замечания сервиса в этой фразе: {html.escape(extra)}</p>"
                if extra
                else ""
            )
            + "</details></article>"
        )
    title = html.escape(data["title"])
    body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{max-width:850px;margin:32px auto;padding:0 20px;background:#faf9f6;
color:#242424;font:17px/1.5 system-ui}}h1{{line-height:1.2}}article{{padding:18px 0;
border-top:1px solid #ddd}}audio{{width:100%;margin:8px 0}}summary{{cursor:pointer}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:8px;
border-bottom:1px solid #ddd}}th{{font-size:14px}}details{{margin:12px 0}}
</style></head><body><h1>{title}</h1>
<p>Сначала прослушай фрагмент, затем открой сценарий и ответ сервиса.
Баллы справочные; решение сервиса и подтверждение на слух показаны отдельно.</p>
{"".join(cards)}<script>document.querySelectorAll('audio').forEach(a=>
a.addEventListener('play',()=>document.querySelectorAll('audio').forEach(b=>
{{if(b!==a)b.pause()}})));</script></body></html>"""
    (output_dir / "index.html").write_text(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    render(args.data, args.output_dir)
