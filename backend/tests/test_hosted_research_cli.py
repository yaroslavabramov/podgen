import json
from types import SimpleNamespace

from podgen import hosted_research_cli as cli
from tests.hosted_research_support import Provider, ready, response


def test_completed_receipt_parse_failure_refreshes_cli_status(monkeypatch, tmp_path, capsys):
    journal, _ = ready(monkeypatch)
    monkeypatch.setenv("PODGEN_DATABASE_URL", "unused-fixture")
    monkeypatch.setattr(
        cli, "Database", lambda _: SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))
    )
    monkeypatch.setattr(cli, "Repository", lambda *args: journal.repo)
    monkeypatch.setattr(cli, "ResearchJournal", lambda *args: journal)
    provider = Provider(response("completed", steps=[]))
    monkeypatch.setattr(cli, "GeminiResearchProvider", lambda _: provider)
    code = cli.main(
        [
            "--project",
            str(journal.project),
            "--actor",
            str(journal.repo.actor_id),
            "--run-id",
            str(journal.run_id),
            "--objects",
            str(tmp_path / "objects"),
            "resume",
            "--export-dir",
            str(tmp_path / "export"),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["state"] == "result_processing_required"
    assert result["code"] == "hosted_research_completed_without_report"
    assert json.loads((tmp_path / "export/status.json").read_text()) == result
    assert len(provider.calls) == 1
