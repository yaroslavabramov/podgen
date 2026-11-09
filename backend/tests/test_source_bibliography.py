"""Bibliography is explicit evidence, not guessed names or a disguised article dump."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.source_bibliography import bibliographic_excerpts, bibliography

ROOT = Path(__file__).resolve().parents[2]


def rain_materials():
    directory = ROOT / "evals/editorial-inputs/experiments/rain-soil"
    packet = EditorialInput.model_validate_json((directory / "shared.json").read_bytes())
    docs = tuple(
        SimpleNamespace(
            path=source.source_path, text=(directory / source.source_path).read_text(), sha256=source.sha256
        )
        for source in packet.documents
    )
    return EditorialMaterials(packet, None, docs)


def test_rain_byline_is_exact_and_the_abstract_and_paths_are_not_bibliography():
    materials = rain_materials()
    values = bibliography(materials)
    assert len(values) == 1
    text = "".join(values[0]["bibliographic_evidence"])
    assert "Zhifei Ge1" in text and "10.1038/ncomms14668" in text
    assert "Aerosolized microorganisms" not in text
    assert "Correspondence and requests" not in text
    assert all(part in materials.documents[-1].text for part in values[0]["bibliographic_evidence"])
    assert materials.documents[-1].path not in json.dumps(values)


def test_no_blank_lines_does_not_expose_body_or_reference_dois():
    header = "ARTICLE\nReceived 1 Jan 2020| Accepted 2 Jan 2020| Published 3 Jan 2020\nA complete title\nAlice Smith1 & Boris Doe1\n"
    body = "Abstract with actual claims.\n" + "Body DOI: 10.9999/not-a-citation\n" * 500
    assert bibliographic_excerpts(header + "DOI: 10.1234/paper\n" + body) == [header, "DOI: 10.1234/paper\n"]
    assert bibliographic_excerpts(header + "DOI:\n" + body) == [header]
    assert bibliographic_excerpts("ARTICLE\nDOI: 10.1234/paper\n" + body) == []
    assert (
        bibliographic_excerpts("Ordinary source text.\nAuthors: A guessed attribution\nDOI: 10.1234/body")
        == []
    )


def test_explicit_headers_are_independent_of_article_layout_and_preserve_complete_lines():
    text = "Title: Complete title\r\n\r\nAuthors: Alice Smith and Борис Doe\r\nDOI: 10.1234/paper\r\n\r\nAbstract\r\nBody text\r\nDOI: 10.9999/reference"
    assert bibliographic_excerpts(text) == [
        "Title: Complete title\r\n",
        "Authors: Alice Smith and Борис Doe\r\n",
        "DOI: 10.1234/paper\r\n",
    ]


def test_structured_metadata_is_preserved_and_absence_is_neutral():
    materials = rain_materials()
    source = materials.packet.documents[-1]
    data = dict(
        title="Complete title",
        authors=["Alice Smith", "Борис Doe", dict(name="Chen", description="ARTICLE BODY MUST NOT LEAK")],
        doi="10.1234/paper",
        text="ARTICLE BODY MUST NOT LEAK",
    )
    text = json.dumps(data)
    raw = materials.packet.model_dump(mode="json")
    raw["documents"][-1]["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    changed = EditorialMaterials(
        EditorialInput.model_validate(raw),
        None,
        (SimpleNamespace(path=source.source_path, text=text, sha256=raw["documents"][-1]["sha256"]),),
    )
    value = bibliography(changed)[0]
    expected = {key: data[key] for key in ("title", "authors", "doi")}
    expected["authors"] = ["Alice Smith", "Борис Doe", dict(name="Chen")]
    assert value["bibliographic_metadata"] == expected
    assert data["text"] not in json.dumps(value)
    missing = bibliography(EditorialMaterials(materials.packet, None, ()))[0]
    assert missing["bibliographic_evidence"] == [] and missing["bibliography_status"] == "not_available"
    assert "unverified" not in json.dumps(missing)
    with pytest.raises(ContractError, match="fact_bibliography_source_mismatch"):
        bibliography(EditorialMaterials(materials.packet, None, changed.documents))
