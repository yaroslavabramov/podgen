# Frozen originals, representations, права и фактическое чтение

Все `.pdf`/`.html` originals получены до извлечения текста. Acquisition JSON хранит actual requested/resolved URL, дату получения и HTTP-исход. Эти метаданные не удостоверяют смысл. Корневой inventory закрепляет bytes всех материалов; SHA-256 проверяется offline.

| Папка / оригинал | Основание сохранения | Что реально прочитано / ограничения |
|---|---|---|
| `gps/nasa-introduction-2017.pdf` | [NTRS](https://ntrs.nasa.gov/citations/20170004590), каталог сохранён: distribution PUBLIC, determinationType GOV_PUBLIC_USE_PERMITTED, containsThirdPartyMaterial false | Слайды 1, 25–38. Слайды 30, 37, 38 дополнительно просмотрены изображениями. Рендер 38 сохранён, так как text layer не содержит уравнения. Исторические данные группировки не взяты как текущие |
| `gps/sps-2020.pdf` | Официальный документ США, титул Distribution Statement A: Approved for public release; distribution is unlimited | §2.2.2.1; §2.4.5; C-1 DOP; C-4 pseudorange/PVT; C-8 скорость света. Остальное сохранено, не заявлено прочитанным |
| `gps/faa-how-it-works.html` | Официальный поясняющий текст федерального ведомства США; сохраняется для исследования с источником | Содержательные абзацы main прочитаны полностью. Навигация extraction сохранена; дата публикации в main неизвестна; актуальные числа группировки и гарантии точности не использованы |
| `snow/snow-1855.html` | John Snow (1813–1858), издание 1855, public-domain текст; [каталог PG](https://www.gutenberg.org/ebooks/72894) и полные [условия PG](https://www.gutenberg.org/policy/license.html) сохранены рядом | Непрерывно с раздела THE CHOLERA NEAR GOLDEN SQUARE, с. 38–52, включая таблицу и метод датировок, титул, примечания транскрибатора. Печатный скан не сверялся. HTML ссылки на внешние карты остаются ссылками; сами изображения карт не загружались и не проверены |

PDF → text: pypdf 6.21.4, `page.extract_text()`; между страницами явный `=== PDF PAGE N ===`, нумерация PDF с 1. HTML → text: BeautifulSoup 4.15.0, встроенный `html.parser`, `get_text('\n', strip=True)`; FAA только main, Snow весь документ. Normalization: UTF-8, LF (CRLF/CR → LF), без дополнительной Unicode нормализации или перефразирования. Английские цитаты в evidence дословны этому представлению, переносы и extraction artifacts сохранены. Русские объяснения — модельный пересказ, не перевод, выданный за цитату.

`evidence.json` хранит локальную метку, полное exact, полуоткрытые Unicode codepoint offsets по сохранённому `.txt`, hash exact и человеческий locator. Это импортные указатели, не backend-generated EvidenceSpan. Их можно восстановить через `tools/build_evidence.py`; неверный диапазон не принимается молча. Формула N1/38 сверяется с PDF/PNG, не с пропустившим её текстом. Таблица Сноу сверена в HTML с заголовками и ключевыми строками, но HTML и TXT зависимы и не заменяют независимого скана.

Частичные representations: формулы/рисунки PDF не все извлеклись; карты Snow не загружены; scripts/styles/nav FAA не входят в main-text. Полные исходные bytes остаются. Source-reading assertions ограничены названными диапазонами: скачивание целого файла не означает чтение каждой страницы. Каталог NASA — metadata/права, каталог PG — metadata/права, не первоисточник причинного объяснения.

Повторное extraction в **копии** партии (сравнить результаты; не менять frozen baseline):

```sh
uv run --no-project --with pypdf==6.21.4 --with beautifulsoup4==4.15.0 python tools/reextract.py
python3 tools/build_evidence.py
```

Команды здесь приведены для cwd в корне копии партии. Для рендера 38 использован PyMuPDF, матрица 1.5×1.5; PNG — вспомогательная representation, исходный PDF закреплён отдельно. Воспроизведение exact text не требует OCR. Экспорт источников — для исследовательских входов; сторонние эмблемы и иллюстрации не присваиваются нашей модельной прозе.

## Уведомление для копий материала Project Gutenberg

This eBook is for the use of anyone anywhere in the United States and most other parts of the world at no cost and with almost no restrictions whatsoever. You may copy it, give it away or re-use it under the terms of the Project Gutenberg License included with this eBook or online at [www.gutenberg.org](https://www.gutenberg.org/policy/license.html). If you are not located in the United States, you’ll have to check the laws of the country where you are located before using this ebook.

Полный локальный текст условий: [gutenberg-license.html](snow/gutenberg-license.html). Оригинал книги сохранён без изменений; извлечение — явно отдельный производный файл. Ссылка на PG в библиографии не обозначает спонсорство или одобрение наших текстов. Дата электронного выпуска 2024 и сообщение транскрибатора о молчаливой правке опечаток не скрыты; издательский текст 1855 и современная representation различаются.
