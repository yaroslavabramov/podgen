"""Prepare the Wright transfer input from visually checked primary-document reproductions."""

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator
from podgen.writing.editorial_contract import EditorialInput

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
INPUT = HERE / "input"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    sources = INPUT / "sources"
    sources.mkdir(exist_ok=True)
    raw = {
        page: (INPUT / f"acquisition/raw-page-{page}.txt").read_text()
        for page in (73, 294, 295, 296)
    }
    diary_first = raw[294].split("Thursday, December 17, 1903", 1)[1]
    diary_last, statement_first = raw[295].split(
        "Document 1-13(b), statement by the Wright brothers", 1
    )
    diary_last = diary_last.removeprefix("Document 1-13(a–b)\n223\n")
    diary = "Thursday, December 17, 1903" + diary_first + diary_last
    statement = statement_first.split("It had not been our intention", 1)[1]
    statement_last = raw[296].removeprefix(
        "Chapter 1: The Achievement of Flight\n224\n"
    )
    statement = "It had not been our intention" + statement + statement_last
    telegram = (
        raw[73]
        .split("Success four flights", 1)[1]
        .split("Orville Wright telegram sent", 1)[0]
    )
    telegram = "Success four flights" + telegram
    texts = {"P1": diary, "P2": statement, "P3": telegram}
    names = {
        "P1": "orville-diary-1903-12-17.txt",
        "P2": "wright-statement-1904-01-05.txt",
        "P3": "orville-telegram-1903-12-17.txt",
    }
    titles = {
        "P1": "Orville Wright — diary, 17 December 1903; NASA SP-2003-4409, document 1-13(a), printed pp. 222–223",
        "P2": "Wilbur and Orville Wright — statement to the Associated Press, 5 January 1904; NASA SP-2003-4409, document 1-13(b), printed pp. 223–224",
        "P3": "Orville Wright — telegram to Milton Wright, 17 December 1903; NASA SP-2003-4409, printed p. 1; Library of Congress mcc.061",
    }
    for key, text in texts.items():
        (sources / names[key]).write_text(text)
    brief = "Расскажи, как братья Райт впервые поднялись в воздух."
    (INPUT / "brief.md").write_text(brief + "\n")

    def quote(document, start, end):
        text = texts[document]
        left = text.index(start)
        right = text.index(end, left) + len(end)
        return {
            "document": document,
            "start": left,
            "end": right,
            "text": text[left:right],
        }

    cards = []

    def card(
        label, facts, excerpts, *, narrative=False, context=None, limitations=None
    ):
        identity = "C" + uuid5(NAMESPACE_URL, "wright-first-flight:" + label).hex[:12]
        value = {
            "card_id": identity,
            "label": label,
            "title": label,
            "claim": facts,
            "story_facts": facts,
            "context": context
            or "Свидетельство участников; первичный текст и дата указаны у выдержки.",
            "limitations": limitations or [],
            "excerpts": excerpts,
        }
        if narrative:
            value["narrative_excerpt_indices"] = [0]
        cards.append(value)
        return identity

    card(
        "Управление передним рулём",
        "Орвилл описывает трудность управления передним рулём: по его оценке, тот был уравновешен слишком близко к центру и сам доворачивался после начала движения. Руль отклонялся чрезмерно то в одну сторону, то в другую; машина резко поднималась, затем ныряла к земле.",
        [quote("P1", "I found the control of the", "ended the flight.")],
        narrative=True,
    )
    card(
        "Сигнал и помощники",
        "Утром 17 декабря братья вынесли машину и подали сигнал людям со станции. Орвилл перечисляет прибывших: Джон Дэниелс, У. С. Доу, А. Д. Этеридж, У. К. Бринкли и Джонни Мур. Позднее присутствовавшие помогали переносить машину обратно к месту старта.",
        [
            quote("P1", "When we got up", "arrived."),
            quote("P1", "With the aid of the station men", "the starting ways."),
        ],
    )
    card(
        "Старт с ровного песка",
        "В публичном сообщении братья помещают старты на ровном песке к западу от лагеря, севернее песчаного холма Килл-Девил в округе Дэр, Северная Каролина. Машина разгонялась по монорельсовому пути и поднималась собственным двигателем, а не за счёт спуска с холма. Полёты шли против ветра.",
        [
            quote("P2", "The starts were all made", "in Dare County, North Carolina."),
            quote(
                "P2",
                "The flights were directly against",
                "limited skill of the operator would\npermit.",
            ),
        ],
    )
    card(
        "Разный ветер и разные скорости",
        "В сообщении братья различают показания приборов на разной высоте: метеостанция измеряла ветер в тридцати футах над землёй, их ручной прибор — в четырёх. Они также различают движение относительно земли и воздуха: при сильном встречном ветре аппарат шёл над землёй заметно медленнее, чем сквозь воздух.",
        [
            quote("P2", "The wind at the time", "last one."),
            quote("P2", "Into the teeth", "through the air."),
        ],
    )
    first = card(
        "Первая попытка этого дня",
        "Орвилл записал, что сел на машину в 10:35 после прогрева двигателя и винтов. Когда отпустили удерживавшую верёвку, аппарат разогнался и сошёл с пути. Дэниелс сделал снимок в момент отрыва. Орвилл оценил полёт примерно в двенадцать секунд и отметил, что секундомер остановили не сразу.",
        [
            quote(
                "P1",
                "After run-",
                "Mr. Daniels took a picture just as it left the tracks.",
            ),
            quote("P1", "Time about 12 seconds", "promptly stopped)."),
        ],
    )
    card(
        "Ремонт и смена пилотов",
        "После первого полёта были сломаны рычаг выключения двигателя и полоз под рулём. Братья отремонтировали машину; около 11:20 второй полёт выполнил Уилбур. Орвилл описывает такое же движение вверх-вниз, несколько большую дальность и примерно прежнее время. Затем около 11:40 снова полетел Орвилл.",
        [quote("P1", "The lever for throwing off", "I made the third trial.")],
    )
    card(
        "Боковой порыв",
        "На третьем полёте, по дневнику Орвилла, порыв слева приподнял левое крыло и сдвинул машину вправо. Он стал работать рулями; при посадке первым коснулось земли левое крыло. Орвилл связал этот неожиданный результат с большей эффективностью бокового управления по сравнению с прежними машинами.",
        [quote("P1", "When out about the same dis-", "probably 12 to 14 feet.")],
    )
    card(
        "Четвёртый полёт",
        "В полдень Уилбур начал четвёртый полёт. По дневнику, после первых колебаний он добился более ровного движения. У песчаного бугорка аппарат снова закачался и резко коснулся земли. Орвилл записал 852 фута над землёй за 59 секунд; передняя рама руля была сильно повреждена, основная рама осталась целой.",
        [quote("P1", "At just\n12 o’clock", "852 feet in 59\nseconds.")],
        narrative=True,
    )
    card(
        "Почему держались низко",
        "Братья объяснили, что ради собственной безопасности решили делать первые испытания близко к земле. Небольшая высота при порывистом ветре оставляла мало пространства для манёвра. Они хотели до возвращения домой проверить мощность двигателя, прочность при посадках и достаточность управления как в ветреную, так и в спокойную погоду.",
        [
            quote(
                "P2",
                "It had previously been decided",
                "Consequently the first flight was short.",
            ),
            quote("P2", "Only those who are acquainted", "as well as in calm air."),
        ],
    )
    card(
        "Порыв уже на земле",
        "Сняв передний руль, участники перенесли машину к лагерю. Пока они обсуждали последний полёт, порыв стал переворачивать аппарат. Все бросились удерживать его; Дэниелс повис внутри и его прокатило вместе с машиной. Орвилл описывает, как тот выбрался из пространства двигателя и цепей, и перечисляет многочисленные повреждения.",
        [quote("P1", "After removing the front rudder", "One spar only was broken.")],
        narrative=True,
    )
    telegram_id = card(
        "Телеграмма домой",
        "После обеда Орвилл и Уилбур отправились в Китти-Хок послать телеграмму отцу Милтону. Переданный текст объявлял четыре полёта с ровного места на собственной тяге, просил сообщить прессе и обещал возвращение к Рождеству. В телеграмме стоит 57 секунд; в дневнике и последующем сообщении братьев для самого длинного полёта указаны 59 секунд.",
        [
            quote("P3", "Success four flights", "home Christmas."),
            quote("P1", "After dinner we went", "the station men."),
        ],
        narrative=True,
        context="Текст телеграммы воспроизведён NASA; существование оригинала и расхождение 57/59 подтверждены каталогом Library of Congress и NPS. Это разные документы одной семьи, не независимые свидетельства.",
    )
    card(
        "Публичное объяснение",
        "5 января 1904 года братья составили сообщение для Associated Press. Они объясняли публикацию тем, что после утечки семейной телеграммы распространились выдуманные истории людей, не видевших полётов. В конце они писали о собственных расходах на опыты и о том, что пока не готовы выдавать фотографии или подробное описание аппарата.",
        [
            quote(
                "P2", "It had not been our intention", "The real facts were as follows:"
            ),
            quote("P2", "From the beginning", "description of the machine."),
        ],
        context="Публичное сообщение участников от 5 января 1904 года; их оценка газетных сообщений атрибутирована им, а не выдана за отдельный аудит прессы.",
    )
    dossier = (
        "# Братья Райт: документы о полётах в декабре 1903 года\n\n"
        + "\n\n".join(
            "## " + value["title"] + "\n\n" + value["story_facts"] for value in cards
        )
        + "\n"
    )
    (INPUT / "dossier.md").write_text(dossier)
    documents = []
    for key, title, path, purpose in [
        ("B1", "Запрос пользователя", "brief.md", "brief"),
        ("D1", "Досье: полёты братьев Райт", "dossier.md", "dossier"),
        *((key, titles[key], "sources/" + names[key], "primary") for key in texts),
    ]:
        documents.append(
            {
                "key": key,
                "title": title,
                "source_path": path,
                "purpose": purpose,
                "sha256": hashlib.sha256((INPUT / path).read_bytes()).hexdigest(),
            }
        )
    packet = {
        "contract": "editorial-input/2",
        "assignment": {
            "key": "wright-first-flight",
            "title": "Первый полёт братьев Райт",
            "question": brief,
            "audience": "Взрослый неспециалист, впервые слышащий эту историю.",
            "listener_outcome": "Понятная история о том, как братья Райт впервые поднялись в воздух.",
            "format": "solo",
            "output_language": "ru",
            "speakers": [
                {
                    "label": "РАССКАЗЧИК",
                    "function": "Рассказывает историю слушателю.",
                    "speech_register": "Живая понятная устная речь.",
                }
            ],
            "duration": {"target_seconds": 360, "min_seconds": 300, "max_seconds": 420},
            "knowledge_cutoff": "2026-11-08T00:00:00Z",
            "required_topics": [],
            "excluded_topics": [],
            "style_guidance": [],
            "listener_profile": "Взрослый неспециалист, впервые слышащий эту историю.",
            "genre": "Короткая документальная история",
        },
        "documents": documents,
        "dossier": {
            "summary": "Первичные документы участников о четырёх испытательных полётах 17 декабря 1903 года у Килл-Девил-Хилс, о наблюдениях управления, работе помощников и сообщениях об испытаниях.",
            "summary_support_labels": [first, telegram_id],
            "throughline_constraints": [],
            "threads": [],
            "knowledge_cards": cards,
        },
    }
    packet = EditorialInput.model_validate(packet).model_dump(mode="json")
    Draft202012Validator(
        json.loads(
            (ROOT / "backend/schemas/editorial-input.v2.schema.json").read_text()
        )
    ).validate(packet)
    save(INPUT / "shared.json", packet)
    print(hashlib.sha256((INPUT / "shared.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
