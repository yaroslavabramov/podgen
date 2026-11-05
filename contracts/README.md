# Canonical contracts v1.0

Владелец: `backend/src/podgen/domain/contracts/registry.py`, 44 artifact kinds по 00–06. `generated/registry.json` указывает владельца и версию; API/model schema views ссылаются на те же canonical definitions. Источник схем редактируется в Python, generated JSON проверяется тестом на точное совпадение.

```sh
uv run --directory backend python -m podgen.schema_export --out ../contracts/generated
node --experimental-strip-types contracts/check-golden.ts
uv run --directory backend python -m podgen.package_cli validate ../contracts/examples/research-note-package.json
```

`golden.json` фиксирует Python/TypeScript JCS/hash/Unicode vectors. `canonical.ts` реализует независимую TypeScript сторону; JSON object keys сортируются по UTF-16, envelope input_refs — множество, payload arrays сохраняют порядок. Decimal values и integers за safe integer range — строки. Null отличается от отсутствующего поля; невалидные Unicode/NaN/Infinity отклоняются.

Пакет `podgen-fixture-v1`:

- `artifacts`: `key`, `kind`, полный `payload`, `input_keys`, `audience`, `identity_of` (null для первой версии).
- `runtime_records`: `key`, `record_kind`, полный `payload`, `audience`.
- `blobs`: `key`, относительный `path`, точные `sha256`, `byte_length`, `media_type`.
- `external_refs` / необязательный `external_runtime_refs`: явные exact refs уже существующего разрешённого scope.
- В payload: `{"$artifact":"key"}`, `{"$runtime":"key"}`, `{"$blob":"key"}` или `{"$text":"relative-file.txt"}`. Локальные пути не выходят из корня пакета.

Дубли, циклы, неизвестные bindings, неверные kind/subtype и hash дают ошибку. Offline validate проверяет schema/ref graph; транзакционный import дополнительно проверяет ACL, existence, selectors и согласованность досье/плана/сценария. Schema pass не выдаёт semantic acceptance.

Сырые WP-02 Markdown/TXT пока принимаются отдельным `editorial-staging-v1` путём; [инструкции](../backend/README.md). Это не альтернативная схема принятых production artifacts.
