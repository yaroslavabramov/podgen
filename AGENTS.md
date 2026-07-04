Краткость - сестра таланта. Пиши всегда очень кратко и по делу.

V2 agent contract:
- Никогда не режь смысловые поля при persistence. Если поле слишком длинное для контракта, верни явную validation error.
- Model-facing preview поля должны быть отдельными compact полями; raw/full смысл остается в persisted workspace/artifacts.
