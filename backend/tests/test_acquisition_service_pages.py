"""Saved Atlantic cable acquisitions: error and service pages are not documents."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from podgen.sources.acquisition.contracts import AcquisitionProfile
from podgen.sources.acquisition.representation import availability
from podgen.sources.extraction.quality.health import assess_web_fetch_health

PAGES = Path(__file__).resolve().parent / "fixtures/acquisition-service-pages"
URLS = {
    "S1": ("https://valentiaisland.ie/history_culture/transatlantic-telegraph-cable/", 200),
    "S5": ("https://www.gutenberg.org/files/46105/46105-h/46105-h.htm", 503),
    "S10": ("https://www.si.edu/es/object/archives/sova-nmah-ac-1386", 403),
    "S13": ("https://www.researchgate.net/publication/3200430_A_history_of_transatlantic_cables", 403),
    "S20": ("https://www.gutenberg.org/files/34765/34765-h/34765-h.htm", 503),
    "S28": ("https://www.americanscientist.org/article/a-wire-across-the-ocean", 503),
}


def classify(source, status=None):
    url, saved_status = URLS[source]
    document = SimpleNamespace(
        status_code=saved_status if status is None else status, requested_url=url, resolved_url=url
    )
    text = (PAGES / f"{source}.txt").read_text()
    return availability(document, SimpleNamespace(text=text), AcquisitionProfile())


@pytest.mark.parametrize("source", ["S5", "S10", "S13", "S20", "S28"])
def test_saved_error_responses_are_unavailable(source):
    state, reasons = classify(source)
    assert state == "unavailable"
    assert "http_status_" + str(URLS[source][1]) in reasons


@pytest.mark.parametrize(
    "source,reason",
    [
        ("S5", "maintenance_page"),
        ("S10", "security_service_block"),
        ("S13", "security_service_block"),
        ("S28", "javascript_challenge"),
    ],
)
def test_the_same_service_pages_are_blocked_even_with_a_success_status(source, reason):
    text = (PAGES / f"{source}.txt").read_text()
    assert reason in assess_web_fetch_health(text, min_chars=1).reasons
    assert classify(source, status=200)[0] == "unavailable"


def test_a_short_real_article_stays_available():
    assert classify("S1") == ("available", [])
