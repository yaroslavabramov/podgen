"""Freeze operator controls before editing the production reader; no provider calls."""

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SOURCE = ROOT / "tmp/snow-edit-nov7/continued-native"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if path.exists():
        assert path.read_bytes() == data, path
    else:
        path.write_bytes(data)


def freeze():
    request_path = next(SOURCE.glob("reader-review-2-*/request.json"))
    base = json.loads(request_path.read_text())
    script = json.loads((SOURCE / "export/revise-2.json").read_text())["output"]
    wire = json.loads(base["input"][0]["content"][0]["text"])
    assert [
        {k: v for k, v in u.items() if k != "index"}
        for u in wire["script"]["utterances"]
    ] == script["utterances"]
    old = (
        ROOT / "backend/src/podgen/writing/prompts/current/reader-review.md"
    ).read_text()
    assert old == base["instructions"]
    role = HERE / "old-reader.md"
    if role.exists():
        assert role.read_text() == old
    else:
        role.write_text(old)
    edited = [
        "В ночь с тридцать первого августа на первое сентября 1854 года в одном лондонском квартале резко растёт число заболевших холерой. Люди заболевают и умирают. Через несколько дней врач Джон Сноу осматривает воду из уличной колонки на Брод-стрит. Он уже подозревает эту воду. За ней сюда ходят многие. Если Сноу прав, опасность расходится по домам в обычных вёдрах. Но пока он может лишь предполагать, что дело именно в колонке.",
        "Вечером третьего сентября Сноу находит в воде совсем немного органической примеси и колеблется. В следующие два дня количество беловатых хлопьев меняется. Может быть, в начале вспышки вода была грязнее. По одному осмотру этого уже не установить. Чтобы проверить подозрение, нужно выяснить, кто брал здесь воду и что происходило с этими людьми. Так поиск переходит от осмотра воды к расспросам.",
        "Для начала Сноу нужен список умерших. Он получает разрешение взять сведения из Главного регистрационного управления. Из восьмидесяти девяти смертей от холеры, зарегистрированных за неделю в трёх подрайонах Лондона, восемьдесят три пришлись на последние три дня. Эти восемьдесят три случая он расследует подробно. В списке есть адреса. Теперь по ним нужно найти тех, кто сможет рассказать об умерших: кто приносил воду, откуда её брали, кто её пил. За записью в ведомости появляется повседневная жизнь семьи.",
        "Почти все смерти произошли недалеко от колонки. Но соседи живут на одной улице, дышат одним воздухом; близость домов ещё не объясняет, что их погубило. Сноу нужно узнать, пили ли умершие воду с Брод-стрит. При этом десять человек жили явно ближе к другим колонкам. Если опасна вода на Брод-стрит, почему погибли люди, которым удобнее было брать её в другом месте?",
    ]
    cases = {}
    cases["01"] = deepcopy(script)
    cases["02"] = deepcopy(script)
    cases["02"]["utterances"][2]["text"] = cases["02"]["utterances"][2]["text"].replace(
        "Из восемьдесят девяти", "Из восьмидесяти девяти"
    )
    for key in ("03", "04"):
        cases[key] = deepcopy(script)
        for u, text in zip(cases[key]["utterances"][:4], edited, strict=True):
            u["text"] = text
    cases["03"]["utterances"][2]["text"] = cases["03"]["utterances"][2]["text"].replace(
        "Из восьмидесяти девяти", "Из восемьдесят девяти"
    )
    cases["05"] = {
        "utterances": [
            {"speaker": "РАССКАЗЧИК", "text": text}
            for text in [
                "Поезд ныряет в тоннель, и окно напротив Ирины становится зеркалом. Она видит своё отражение и коробку на коленях. В коробке — отцовский радиоприёмник. Неделю назад отец собрался его выбросить: крутил ручку настройки, а слышал только треск. Ирина забрала приёмник и отнесла в мастерскую.",
                "Теперь надо довезти его домой. На поворотах коробка съезжает с колен, Ирина придерживает её ладонью. Выйдя из тоннеля, поезд проходит мимо дач, потом за окном появляются склады. До станции осталось совсем немного.",
                "Дома Ирина ставит коробку на кухонный стол. Отец снимает упаковочную бумагу, включает приёмник и поворачивает ручку. Сквозь короткий шум пробивается музыка. Он оставляет руку на колёсике настройки и слушает. Ирина развязывает шарф и вешает его на спинку стула.",
            ]
        ]
    }
    spec = {
        "01": {
            "source": "archived full episode",
            "grammar": "required local correction",
            "opening_style": "required passage/sequence repair",
        },
        "02": {
            "source": "archived full episode; only declension repaired",
            "grammar": "no finding",
            "opening_style": "required passage/sequence repair",
        },
        "03": {
            "source": "edited first four utterances; archived declension retained",
            "grammar": "required local correction",
            "opening_style": "no required finding",
        },
        "04": {
            "source": "edited first four utterances",
            "grammar": "no finding",
            "opening_style": "no required finding",
        },
        "05": {
            "source": "original fictional transfer control",
            "grammar": "no finding",
            "opening_style": "no required finding",
            "metaphor": "train diving into a tunnel; window becoming a mirror; preserve unless an actual contextual defect is shown",
        },
    }
    criteria = {
        "frozen_before_prompt_change": True,
        "hypotheses": [
            "A coherent narrative move can still require correction of its wording; objective grammatical errors need local correction."
        ],
        "meanings_to_preserve": [
            "Suspicion followed by inconclusive water inspection, then a death register and household inquiries.",
            "The unexplained distant-household cases lead into the unchanged next passage.",
        ],
        "style_target": "The opening accumulates personified detective language and staged verdicts. Request a concrete repair while preserving the move from inspection to inquiries, rather than praising the move as proof of ready prose.",
        "no_forced_whole_episode_decision": True,
        "unaffected_text": "For cases 01–04, utterances from index 4 onward are byte-identical.",
        "scope": "Human editorial criteria remain outside every model request. Calls are independent script_only Terra medium readings, never comparisons.",
        "bounded_live_plan": {
            "initial_new_calls": 9,
            "max_followup_calls": 3,
            "old_archival_reading_reused_for": "01",
        },
        "cases": spec,
    }
    write(HERE / "criteria.json", criteria)
    for key, value in cases.items():
        write(HERE / "texts" / f"{key}.json", value)
    write(HERE / "archive/request.json", base)
    write(
        HERE / "archive/review-record.json",
        json.loads((SOURCE / "export/reader-review-2.json").read_text()),
    )
    receipt = next(SOURCE.glob("reader-review-2-*/receipt.json"))
    write(HERE / "archive/receipt.json", json.loads(receipt.read_text()))
    manifest = {
        "source_request": str(request_path.relative_to(ROOT)),
        "old_prompt_sha256": sha256(old.encode()).hexdigest(),
        "case_sha256": {
            k: sha256((HERE / "texts" / f"{k}.json").read_bytes()).hexdigest()
            for k in cases
        },
        "criteria_sha256": sha256((HERE / "criteria.json").read_bytes()).hexdigest(),
    }
    write(HERE / "frozen.json", manifest)
    print(
        json.dumps(
            {
                "cases": len(cases),
                "opening_words": {
                    key: sum(len(u["text"].split()) for u in value["utterances"][:4])
                    for key, value in cases.items()
                },
                "frozen": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    freeze()
