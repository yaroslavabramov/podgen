"""Rebuild the frozen source-restoration input from a retained pre-revision packet and exact sources."""

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from podgen.domain.canonical import digest
from podgen.writing.editorial_contract import EditorialInput

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "evals/editorial-inputs/current/snow-pump"
CORPUS = ROOT / "evals/cases/development/wp02-batch-01"
PREVIOUS = CASE / "history/before-source-restoration-2026-11-08.json"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    before = json.loads(PREVIOUS.read_text())
    data = deepcopy(before)
    text = (CORPUS / "sources/snow/snow-1855.txt").read_text()

    def excerpt(start, end):
        left = text.index(start)
        right = text.index(end, left) + len(end)
        return {"document": "D3", "start": left, "end": right, "text": text[left:right]}

    cards = {card["label"]: card for card in data["dossier"]["knowledge_cards"]}
    water = cards["Осмотр воды"]
    water["claim"] = water["story_facts"] = (
        "Сноу относит резкий рост заболевания к ночи 31 августа — 1 сентября. Узнав положение и границы вспышки, "
        "он заподозрил загрязнение воды часто посещаемой колонки на Брод-стрит. Вечером 3 сентября при осмотре "
        "нашёл так мало органической примеси, что заколебался с выводом. Затем, по его рассказу, дальнейшие "
        "расспросы показали: кроме воды этой колонки, он не находил другого обстоятельства или агента, общего "
        "для ограниченной зоны резкого увеличения холеры и не распространявшегося за её пределы. "
        "В следующие два дня количество заметной при внимательном осмотре органической примеси в виде мелких "
        "белых хлопьев менялось. Из этой изменчивости он допустил, что в начале вспышки вода могла быть ещё "
        "грязнее. Хлопья — наблюдение примеси, а более раннее состояние воды — его предположение; возбудитель "
        "этим осмотром не был установлен."
    )
    water["context"] = (
        "Сноу описывает проверку подозрения, возникшего из положения и границ вспышки. Малое количество "
        "органической примеси вызвало колебание; дальнейшие расспросы и изменчивость воды продолжили проверку, "
        "а не дали окончательного оправдания или обвинения по одному образцу. Утверждение об отсутствии "
        "другого общего местного агента принадлежит Сноу; это не независимо доказанное исключение всех причин."
    )
    delivery = cards["Доставка воды"]
    delivery["excerpts"] = [
        excerpt(
            "Dr. Fraser also first called my attention",
            "did not suffer, or, at least, not severely.",
        )
    ]
    delivery["claim"] = delivery["story_facts"] = (
        "В книге 1855 года Сноу пишет, что доктор Фрейзер первым обратил его внимание на обстоятельства "
        "доставки воды в Хэмпстед. Сын умершей женщины сообщил Сноу: она много месяцев не бывала в районе "
        "Брод-стрит, но ежедневно ходившая оттуда в Уэст-Энд повозка обычно привозила ей большую бутыль "
        "предпочитаемой воды из колонки. Воду взяли в четверг 31 августа; женщина пила её вечером и в пятницу, "
        "заболела в пятницу вечером и умерла в субботу 2 сентября. Племянница тоже пила воду, вернулась "
        "в Ислингтон, заболела холерой и умерла. По Сноу, в это время холеры не было ни в Уэст-Энде, "
        "ни в окрестностях дома племянницы. Пившая воду служанка не заболела либо, по крайней мере, "
        "не заболела тяжело. Запись о смерти женщины Сноу цитирует из выпуска регистра за 9 сентября."
    )
    delivery["context"] = (
        "Фрейзер указал Сноу на эти обстоятельства; сведения о привычке и доставке переданы со слов сына, "
        "а сведения о смерти — также через цитату из регистра. Болезнь после питья воды вне поражённого "
        "квартала и отсутствие местной холеры — существенный контраст в рассуждении Сноу. Дата события "
        "не устанавливает дату, когда этот аргумент стал известен ему или совету."
    )
    theory = {
        "card_id": "C0e414a5f3c87",
        "label": "Гипотеза передачи",
        "title": "Сноу объясняет свой путь к гипотезе",
        "claim": (
            "В предисловии к изданию 1855 года Сноу указывает, что первая версия его труда вышла в августе "
            "1849 года. В книге он выводит предполагаемый путь передачи из начала болезни в пищеварительном "
            "тракте: вызывающее холеру вещество, по его рассуждению, случайно проглатывается и размножается "
            "в желудке и кишечнике. Он рассматривает смешение выделений больных с питьевой водой, в том числе "
            "просачивание через грунт в колодцы, как путь распространения. При этом он признаёт, что структуру "
            "предполагаемого холерного вещества нельзя распознать под микроскопом: он судит о нём по эффектам."
        ),
        "context": (
            "Это изложение гипотезы и аргументации самого Сноу в издании 1855 года, а не сообщение об "
            "обнаружении возбудителя. Дата первого издания не означает, что каждая фраза второго уже была "
            "напечатана в 1849 году. Общее объяснение передачи и конкретное подозрение в отношении Брод-стрит "
            "нужно различать."
        ),
        "limitations": [
            "Наблюдение органической примеси не идентифицирует описанное Сноу болезнетворное вещество.",
            "Наличие общего объяснения передачи не доказывает загрязнение конкретной колонки в любой день.",
        ],
        "excerpts": [
            excerpt(
                "The first edition of this work, which was published",
                "in the medical journals.",
            ),
            excerpt(
                "As\ncholera commences with an affection",
                "interior of the stomach\nand bowels.",
            ),
            excerpt(
                "It is no objection to\nthis view", "not by their physical properties."
            ),
            excerpt(
                "If the cholera had no other means of communication",
                "supplied with water.",
            ),
        ],
    }
    theory["story_facts"] = theory["claim"]
    data["dossier"]["knowledge_cards"].append(theory)
    narrative_selection = {
        "Гипотеза передачи": [1, 3],
        "Осмотр воды": [0],
        "Доставка воды": [0],
        "Запись и мать": [0],
        "Осмотр Йорка": [1, 4],
    }
    for card in data["dossier"]["knowledge_cards"]:
        if card["label"] in narrative_selection:
            card["narrative_excerpt_indices"] = narrative_selection[card["label"]]

    old_dossier = (CORPUS / "author/snow-pump/dossier.md").read_text()
    supplement = (
        "## Восстановленные основания и наблюдения\n\n"
        + theory["story_facts"]
        + " [Книга Сноу, предисловие, с. 15, 22–23.]\n\n"
        + water["story_facts"]
        + " [Книга Сноу, с. 38–39.]\n\n"
        + delivery["story_facts"]
        + " [Книга Сноу, с. 44–45.]\n\n"
        + "Эти сведения возвращают основания проверки и источниковую цепочку. Они не задают начало, порядок "
        "рассказа или обязательный вывод для планировщика. Полные дословные выдержки и их диапазоны "
        "сохранены в карточках текущего shared; исходная книга и приходской отчёт не изменены. "
        "Перечень чтения в сохранённом разделе ниже относится к версии 2. При обновлении дополнительно "
        "проверены предисловие и приведённые места с. 15, 22–23, 38–39 и 44–45; полный текст сверен "
        "с повторным извлечением из сохранённого HTML. Новая сверка не является сверкой печатного скана.\n\n"
    )
    dossier = old_dossier.replace(
        "# Досье: решение у колонки и предел причинного вывода",
        "# Досье: Джон Сноу и вспышка холеры в Лондоне",
        1,
    ).replace("## Что можно рассказать", supplement + "## Что можно рассказать", 1)
    dossier = dossier.replace(
        "../../sources/snow/", "../../../cases/development/wp02-batch-01/sources/snow/"
    )
    dossier = dossier.replace(
        "Модельный исследовательский текст, версия 2 от 05.11.2026.",
        "Исследовательский текст редакторского чата, версия 3 от 08.11.2026; сохранён материал версии 2 от 05.11.2026.",
        1,
    )
    (CASE / "dossier.md").write_text(dossier)
    document = next(doc for doc in data["documents"] if doc["key"] == "D2")
    document.update(
        title="Исследовательское досье: Джон Сноу и вспышка холеры в Лондоне",
        source_path="editorial-inputs/current/snow-pump/dossier.md",
        sha256=sha256(dossier.encode()).hexdigest(),
    )
    data = EditorialInput.model_validate(data).model_dump(mode="json")
    write(CASE / "history/before-percolation-2026-11-08.json", data)
    write(
        CASE / "source-restoration.json",
        {
            "contract": "editorial-data-revision/1",
            "date": "2026-11-08",
            "previous_input": str(PREVIOUS.relative_to(CASE)),
            "previous_sha256": sha256(PREVIOUS.read_bytes()).hexdigest(),
            "previous_digest": digest(before),
            "target_digest": digest(data),
            "modified_card_ids": [
                water["card_id"],
                delivery["card_id"],
                cards["Запись и мать"]["card_id"],
                cards["Осмотр Йорка"]["card_id"],
            ],
            "added_card_ids": [theory["card_id"]],
            "narrative_excerpt_selections": narrative_selection,
            "unchanged_primary_documents": [
                doc["key"] for doc in data["documents"] if doc["purpose"] == "primary"
            ],
        },
    )


if __name__ == "__main__":
    main()
