# Источник Сноу

John Snow, *On the Mode of Communication of Cholera*, 2nd edition, London: John Churchill, 1855. [Оригинал полученной HTML-транскрипции](snow-1855.html); [каталог](catalog.html); [полная лицензия PG](gutenberg-license.html). Электронный выпуск — 7 февраля 2024; получение — в acquisition JSON, отдельно от исторических дат. Автор умер в 1858; используемый текст издания 1855 — public-domain материал. Сохранены START/END, титул и примечания транскрибатора; он сообщает о молчаливом исправлении опечаток и перестановке сносок. Это не проверка печатного скана.

Прочитан непрерывный раздел с. 38–52 от THE CHOLERA NEAR GOLDEN SQUARE, титул и примечания. Таблица I прочитана с заголовками/методом датировок; ключевые строки сверены с HTML. `.txt` извлечён BeautifulSoup 4.15.0 / html.parser / get_text с newline и strip, UTF-8 LF, без дополнительной Unicode нормализации. [Опоры S1–S9](evidence.json) — exact и offsets Unicode code points `[start,end)` в `.txt`, не PDF/HTML bytes и не ArtifactRef.

Карта в исходном HTML дана внешними ссылками на изображения: сами изображения не загружены, не сверены и не перенесены в сценарий. Досье использует описание карты самим Сноу. Источник — один авторский труд участника событий; цитируемые им свидетели и регистры в оригинале отдельно не получены. Не выдавать извлечение и HTML за независимые подтверждения.

## Уведомление Project Gutenberg

This eBook is for the use of anyone anywhere in the United States and most other parts of the world at no cost and with almost no restrictions whatsoever. You may copy it, give it away or re-use it under the terms of the Project Gutenberg License included with this eBook or online at [www.gutenberg.org](https://www.gutenberg.org/policy/license.html). If you are not located in the United States, you’ll have to check the laws of the country where you are located before using this ebook.

[Полный локальный текст условий](gutenberg-license.html) передаётся вместе с копией. Оригинальный HTML не изменён; производный текст обозначен отдельно. Библиографическая ссылка не означает одобрения Project Gutenberg наших материалов.

## Корпус snow-sources-2

Добавлен [оригинальный скан приходского отчёта 1855](parish-1855-b21363870.pdf), 192 PDF-страницы, [метаданные IA](parish-1855-ia-metadata.json) с Public Domain Mark. [API Wellcome work](wellcome-work.json) относится к b22327460, другому экземпляру; загружен разрешённый альтернативный b21363870 из LSHTM. [Индекс версии](corpus-v2.json) закрепляет источники, representations и авторские inputs. Предыдущая версия — Git b449d4d.

Прочитаны PDF 3, 5–10, 93–94, 125–127, 161–174, 179–183; визуально сверены 3, 5, 93–94, 126, 163–164, 167–171, 174, 179–183. Рендеры сохранены (PyMuPDF 2.1, scale 1.4). Полный OCR/text layer извлечён pypdf 6.21.4, UTF-8/LF. Не объявляется проверенным весь скан. Даты 1854/April 3rd/May 30th/June 5th 1855 в OCR повреждены; в сценарий взяты визуальные значения. Таблицы, планы и карты не используются как полностью сверенная новая статистика/топография.

Новое знание: поздний опрос матери ребёнка, поручение комиссии 3 апреля, осмотр стока/ямы/колодца в конце апреля, медицинское возражение Роджерса и ограничения Уайтхеда. Семейство общее: Сноу участвовал в комиссии, Йорк обследовал по следам Уайтхеда, Роджерс уточнял память у матери. Документ уточняет физический путь; диагноз ребёнка и эффект закрытия не становятся бесспорными. Метки S10/W/Y/P — локальные импортные указатели.
