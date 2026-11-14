import json

from sqlalchemy import event

from podgen import episode_cli
from podgen.episodes.production import providers
from podgen.episodes.production.prepare import prepare
from tests.episode_production_support import setup_episode


def test_cli_start_uses_real_service_and_exports_master_then_status_controls_are_provider_free(
    repo, scope, producer, monkeypatch, tmp_path, capsys
):
    config, _, hosted, models, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    # CLI source retrieval uses the existing transport; the source double is the
    # same free local fetcher used by the production service integration.
    from podgen.sources.acquisition.http import HttpSourceFetcher
    from tests.source_acquisition_support import document

    async def fetch(self, url, profile, *, headers=None):
        return document(url=url)

    monkeypatch.setattr(HttpSourceFetcher, "fetch", fetch)
    monkeypatch.setattr(providers, "credentials", lambda *args: None)
    monkeypatch.setattr(providers, "research_provider", lambda settings: hosted)
    monkeypatch.setattr(providers, "model_provider", lambda profile: models)
    monkeypatch.setattr(providers, "audio_transport", lambda: speech)
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            dict(
                project=str(scope[0]),
                actor=str(repo.actor_id),
                objects=str(repo.objects.root),
                database_url=repo.database.engine.url.render_as_string(hide_password=False),
            )
        )
    )
    settings = tmp_path / "settings.json"
    settings.write_text(config.settings.model_dump_json())
    common = ["--reference-state", str(state), "--run-id", str(config.run_id)]
    assert (
        episode_cli.main(
            [
                *common,
                "start",
                "--topic",
                config.topic,
                "--settings",
                str(settings),
                "--export-dir",
                str(tmp_path / "episode"),
            ]
        )
        == 0
    )
    assert (tmp_path / "episode/audio/master.wav").exists()
    sent = (len(hosted.calls), len(models.requests), len(speech.calls))

    def forbidden(*args, **kwargs):
        raise AssertionError("completed/status/control path constructed a provider")

    for name in ("credentials", "research_provider", "model_provider", "audio_transport"):
        monkeypatch.setattr(providers, name, forbidden)
    for operation in ("status", "pause", "resume", "run"):
        assert episode_cli.main([*common, operation]) == 0
    assert sent == (len(hosted.calls), len(models.requests), len(speech.calls))
    captured = capsys.readouterr().out
    assert "database_url" not in captured and "postgresql" not in captured


def test_status_is_one_bounded_read_and_has_no_publications(repo, scope, producer, monkeypatch):
    config, runner, *_ = setup_episode(repo, scope, producer, monkeypatch)
    prepare(repo, scope[0], config)
    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(repo.database.engine, "before_cursor_execute", record)
    try:
        for _ in range(3):
            assert runner.journal.read()["revision"] == 0
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", record)
    assert sum("SELECT r.* FROM episode_production_runs" in sql for sql in statements) == 3
    assert not any(sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
