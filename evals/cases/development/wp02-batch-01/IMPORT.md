# Временная файловая поставка до WP-01

Цель — передать полный смысл, оригиналы и местные ссылки, не создать параллельную каноническую схему. Markdown/TXT — материалы; JSON evidence — индекс извлечённых локальных выдержек; inventory — контроль bytes. Ни один местный label, path, filename или hash не объявляется ArtifactRef/UUID. Нет локальных копий EpisodeRun/AcceptanceRecord/ReleaseManifest. Авторская разметка не выдаётся за backend ReadReceipt или independent report.

База всех записей `sources/...` в текстах — корень этой партии (или корень author-export, сохраняющий дерево). Ссылки на brief/plan/dossier внутри одной папки обычные относительные. После канонического импорта таблица соответствия старого path+sha256 → exact revision хранится импортёром, не дописывается в старые frozen файлы.

| Материал | Куда преобразовать по принятой версии WP-01 |
|---|---|
| `author/<case>/brief.md` | EpisodeSpec: language RU, формат dialogue/solo, duration target 900 с, временной объект и аудитория. ID/даты импортного события назначает backend; дата написания/предмета остаётся происхождением |
| `sources/*` originals + acquisition | SourceRevision и AcquisitionRecord с actual bytes/хешем/датами/rights. Catalog/permission metadata не становятся самостоятельным смысловым подтверждением механизма |
| Сохранённые `.txt`, HTML-таблица и рендер 38 | SourceRepresentation с pinned extraction config и coverage/warnings. Не скрывать формулу, пропущенную text extraction, или несохранённые карты HTML |
| `sources/*/evidence.json` | Проверить exact match по полной representation и импортировать EvidenceSpan с immutable refs; локаторы страниц сверить с оригиналами. Существующие offsets — Unicode code points `[start,end)` в UTF-8 LF representation, не позиции PDF/HTML bytes |
| `dossier.md` | ResearchNote(s), citations с локальных G/S на созданные spans; limitations/interpretations сохранить целиком. DossierSnapshot собирает canonical builder, не этот текст |
| `plan.md` | Кандидат StoryPlan, роли, переходы и оговорки; модельное происхождение сохранить. Это не экспертный план, пока эксперт реально его не проверил |
| `draft.txt`, `final.txt` | Отдельные ScriptRevision; label спикера отделяется от произносимого text. Первая версия parent=null/ChangeSet=null. Крупные добавления и перестройки требуют identity mapping при импорте, не совпадения ordinal IDs |
| `revision-notes.md`, `source-check.md` | Авторские материалы процесса, не независимые approvals. Нельзя конвертировать самопроверку в VerificationReport с независимым producer |
| `evaluator-only/*/expected-understanding.md` | Кандидат EvaluationAnnotation с evaluator ACL и явным model annotator. Подтвердить scope и refs; не присваивать человеку работу модели |
| `experiment.md`, задания | Вход для versioned EvaluationCase/scoring/prompt configs после валидации. Не заносить планируемые попытки как выполненные EvaluationResult |

Импорт выполняется сначала в evaluation purpose. При ошибке типа, длины, локатора или missing ref — явная ошибка с файлом/полем; запрещены усечение смысловых полей, молчаливое опускание последнего абзаца и подстановка summary. Если материалы не помещаются в один canonical artifact, разделение должно сохранять полный текст и связи. Model-facing previews создаются отдельно после persistence.

До допуска M1: (1) WP-01 schemas/refs и round-trip; (2) WP-04 exact evidence/source closure; (3) реальный ACL/изоляция; (4) независимая проверка текста и источников; (5) нейтральная читка и реальное измерение; (6) критерии/человеческий контроль по EXP-04. Пока это входные материалы для development, не зарегистрированные и допущенные fixtures. Нельзя подменить недоступный скан уверенным флагом original_inspection.

Редакция 2: sources/snow/corpus-v2.json — индекс точных файлов и авторских inputs, не канонический manifest. Он заменяет source access версии 1 для новых назначений. При импортировании не сливать разные экземпляры Wellcome b22327460 и IA b21363870 или этапы сентябрьского решения/апрельской находки. Содержательные сравнения разных корпусов помечать отдельно. Сообщённый CPO тайминг не превращать в AudioQAReport или HumanAssessment без реальных артефактов.
