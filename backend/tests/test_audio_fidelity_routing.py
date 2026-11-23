"""Review routing of saved Atlantic cable discrepancies."""

import pymorphy3
import pytest

from podgen.audio.fidelity.routing import route

MORPH = pymorphy3.MorphAnalyzer(lang="ru")


def candidate(expected, heard):
    tokens = lambda text: [dict(normalized=word) for word in text.casefold().split()]  # noqa: E731
    return dict(kind="script_discrepancy", expected_tokens=tokens(expected), recognized_tokens=tokens(heard))


@pytest.mark.parametrize(
    "expected,heard,result",
    [
        ("", "все осложняется тем что крюка надо два", "automatic_repair"),
        ("кабель уходит обратно в воду", "кабель", "automatic_repair"),
        ("материку", "материк", "recognition_variant"),
        ("правительственная", "правительственное", "recognition_variant"),
        ("отдельное крепление", "отдельные крепления", "recognition_variant"),
        ("уайлдман уайтхаус", "wildman whitehouse", "recognition_variant"),
        ("мэтью", "мэью", "user_confirmation"),
        ("о весе миля", "вес метр", "user_confirmation"),
        ("просмоленной", "промасленной", "user_confirmation"),
        ("изамбард", "из анборт", "user_confirmation"),
    ],
)
def test_routes_follow_the_kind_of_difference(expected, heard, result):
    assert route(candidate(expected, heard), MORPH) == result


def test_timing_uncertainty_goes_to_a_person():
    assert route(dict(kind="recognition_uncertainty"), MORPH) == "user_confirmation"
