# WP-02 · первая партия development

Два полных модельных комплекта для содержательного ревью письма. Дата подготовки: 5 ноября 2026. Это 2 из планируемых 12 development-тем, 2 из начальных 36 тем. Calibration и holdout не открывались и не использовались. Семейства GPS/время и Сноу/Брод-стрит закреплены за development; связанные пересказы, переводы и соседние вопросы не переносить в другие splits.

## Что читать

| Case | Открытые входы автора | Полные тексты: только оценщику | Правки и основания |
|---|---|---|---|
| `gps-clock`: механизм, RU dialogue, цель 15 мин | [Бриф](author/gps-clock/brief.md) · [Досье](author/gps-clock/dossier.md) · [План](author/gps-clock/plan.md) | [Первый полный черновик](evaluator-only/gps-clock/draft.txt) · [Завершённая редакция](evaluator-only/gps-clock/final.txt) | [Причины правок / второй ведущий](evaluator-only/gps-clock/revision-notes.md) · [Самопроверка оснований](evaluator-only/gps-clock/source-check.md) |
| `snow-pump`: историческое решение, RU solo, цель 15 мин | [Бриф](author/snow-pump/brief.md) · [Досье](author/snow-pump/dossier.md) · [План](author/snow-pump/plan.md) | [Первый полный черновик](evaluator-only/snow-pump/draft.txt) · [Завершённая редакция](evaluator-only/snow-pump/final.txt) | [Причины правок](evaluator-only/snow-pump/revision-notes.md) · [Самопроверка оснований](evaluator-only/snow-pump/source-check.md) |

[Оригиналы, права и чтение](sources/README.md) · [Доступ](ACCESS.md) · [Импорт после WP-01](IMPORT.md) · [Происхождение / ограничения](PROVENANCE.md) · [Протокол сравнения EXP-04](evaluator-only/experiment.md) · [Первое чтение](evaluator-only/task-first-read.md) · [Независимый фактчек](evaluator-only/task-factcheck.md).

## Воспроизведение локального материала

Из корня репозитория, Python 3.11+; для выдачи пакетов сторонние библиотеки не нужны:

```sh
python3 evals/cases/development/wp02-batch-01/tools/prepare.py verify
python3 evals/cases/development/wp02-batch-01/tools/prepare.py export-author --case gps-clock --output /tmp/wp02-author-gps
python3 evals/cases/development/wp02-batch-01/tools/prepare.py export-author --case snow-pump --output /tmp/wp02-author-snow
python3 evals/cases/development/wp02-batch-01/tools/prepare.py export-first-read --case gps-clock --stage final --blind-id K7 --output /tmp/wp02-reader-K7
```

Папка назначения должна отсутствовать. Автору передать только результат `export-author`, первому читателю — только результат `export-first-read`; не весь checkout. Для выдачи только префикса добавить, например, `--start 1 --stop 12`; числа — номера реплик, не акты. После сохранения реакции контроллер выдаёт следующее окно; последняя реплика окна должна быть прочитана до открытия будущего. Передача целого файла означает retrospective first-break, если отзыв сформирован после полного чтения. Версии/анонимные назначения хранит организатор отдельно от выдачи.

`verify` сверяет полную инвентаризацию bytes, exact evidence offsets, структуру всех реплик, числовые примеры и размеры текстов. Это проверка комплектности, не автоматическая оценка качества. Повторное построение выдержек: `python3 .../tools/build_evidence.py`; оно детерминировано по frozen representations. Оригиналы доступны без сети; обновлённая загрузка не заменяет их в существующем опыте.

Завершённая редакция GPS: 1 908 слов; Сноу: 1 815, счёт по пробельным токенам без меток спикеров. **До читки** условные диапазоны при 115–145 словах/мин: 13,2–16,6 и 12,5–15,8 минуты. Это сценарные оценки с некалиброванным темпом; фактическое аудио, длительность и паузы не измерены. Черновики короче: 1 451 и 1 425 слов, что отмечено в причинах правок.

## Статус и следующие проверки

Оба финала написаны целиком, без placeholders. Все авторские/исследовательские тексты и разметка созданы моделью в этой сессии. Профессиональные автор/редактор не участвовали; независимый первый читатель и фактчекер не назначались; нейтральной читки и слушательских результатов нет. Авторская проверка ключевых опор выполнена и отделена от будущей независимой проверки. `final` означает завершённую авторскую редакцию, не допуск production.

До WP-01/04 это файловый импортный пакет, не EvaluationCase/ArtifactRevision. Нет канонического runner, ACL-приёмки AC-17, результатов AC-18/M1, профилей qualification или release bundle. Перед M1 нужны канонический импорт, проверенные refs/spans/support closure, независимый source/editorial audit, профессиональный контроль, реальная одинаковая читка и слушатели по плану. Эта законченная партия материалов не закрывает полный WP-02.

Существенные неопределённости: [у GPS](author/gps-clock/dossier.md) — реальная геометрия/точность устройств и восприятие объяснения; [у Сноу](author/snow-pump/dossier.md) — единственный первичный труд в транскрипции без сверки скана, знания/мотивы совета и величина эффекта закрытия. Неизвестный эффект не равен нулевому.
