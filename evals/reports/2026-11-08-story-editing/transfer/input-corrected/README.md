# Исправленный Wright first-flight input

Shared SHA-256: `b2a35b11118e60576a2873e935fcb65b847ea86c3ba47e60fd21dd371773fd80`.

Досье — `dossier.md`. В карточке «Четвёртый полёт» 852 фута явно обозначены как расстояние относительно земли, пройденное за 59 секунд. Первичные документы и идентификаторы фактов не изменены. Исходный эксперимент сохранён в соседнем `../input/` с прежним SHA; [связь версий и воспроизведение](../INPUT-CORRECTION.md).

## Штатный импорт

Все `source_path` в этом `shared.json` относительны **этой папке**:

| Key | `source_path` / файл | `purpose` |
|---|---|---|
| B1 | `brief.md` | `brief` |
| D1 | `dossier.md` | `dossier` |
| P1 | `sources/orville-diary-1903-12-17.txt` | `primary` |
| P2 | `sources/wright-statement-1904-01-05.txt` | `primary` |
| P3 | `sources/orville-telegram-1903-12-17.txt` | `primary` |

Допустить все пять файлов через `editorial-source-import` с одним **новым** scope, например `wright-distance-corrected-2026-11-08`, соответствующими `--purpose` и точными `--source-path`. Сохранить полученные refs в новую карту `source-map.json`. Затем обычным CLI с параметрами проекта, пользователя и хранилища:

```text
editorial-import --input INPUT_CORRECTED_DIR/shared.json --source-map NEW_SOURCE_MAP --out NEW_SHARED_REF
```

Кейс: `wright-first-flight`, режим `own-plan`. Старые refs эксперимента не подменяются; для запуска нужен новый `run-id`. Импорт не вызывает модели.

Точные атрибуции и происхождение — `provenance.json`, контрольные хеши — `validation.json`. PDF единственный: [прежнее приобретение](../input/acquisition/nasa-sp-2003-4409-vol1.pdf). В этом пакете PDF не дублируется. Платные вызовы при исправлении не выполнялись.
