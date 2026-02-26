from app.ingestion.normalize import normalize_text
from app.models.source import SourceType


def test_normalize_text_removes_control_chars_for_all_sources() -> None:
    raw = "A\x01B\x02C\x0bD\x0cE\x1fF"

    normalized = normalize_text(raw, SourceType.WEB)

    assert normalized == "ABCDEF"


def test_normalize_text_applies_pdf_transforms_for_web_pdf_profile() -> None:
    raw = "inter-\nnational\n3\ncurrency"

    normalized = normalize_text(raw, SourceType.WEB, extraction_profile="web_pdf_direct")

    assert "international" in normalized
    assert "\n3\n" not in f"\n{normalized}\n"


def test_normalize_text_does_not_apply_pdf_transforms_for_regular_web_html() -> None:
    raw = "inter-\nnational\n3\ncurrency"

    normalized = normalize_text(raw, SourceType.WEB, extraction_profile="trafilatura")

    assert "inter-\nnational" in normalized
    assert "\n3\n" in f"\n{normalized}\n"
