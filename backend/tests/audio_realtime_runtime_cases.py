"""Real persistence/CLI graph with HTTP and WebSocket peers doubled."""

import json
from uuid import uuid4

from podgen import audio_adjudication_cli
from podgen.runtime.evaluation.audio.adjudication.attempts import history
from podgen.runtime.evaluation.audio.adjudication.contracts import prefix
from tests.audio_adjudication_doubles import configuration as http_configuration
from tests.audio_realtime_cases import configuration
from tests.test_audio_realtime_runtime import routed


def assert_persisted_realtime(journal, old_config, common, folder, monkeypatch):
    config = configuration(http_configuration(old_config.model_copy(update=dict(review_id=uuid4()))))
    path, destination = folder / "review-v3-config.json", folder / "review-v3-export"
    path.write_text(config.model_dump_json())
    args = ["--config", str(path), "--export-dir", str(destination)]
    transport, client, primary, secondary = routed(config)
    monkeypatch.setenv("JEMINI_API_KEY", "private-review-key")
    monkeypatch.setenv("OPENAI_API_KEY", "private-openai-key")
    monkeypatch.setattr(audio_adjudication_cli, "ReviewerTransport", lambda config: transport)
    try:
        assert audio_adjudication_cli.main([*common, "prepare", *args]) == 0
        assert not primary and not secondary
        assert audio_adjudication_cli.main([*common, "run", *args]) == 0
        manifest = journal.get(prefix(config.review_id) + ":manifest")
        assert len(primary) == 1 and len(secondary) == len(manifest["windows"])
        for window in manifest["windows"]:
            saved = journal.get(window["key"])
            projected = saved["providers"]["openai"]
            assert journal.bytes(window["key"], projected["transport_audio_blob"])
            receipt = history(journal, window["key"])[-1]["receipt"]
            assert receipt["handshake_status"] == 101 and receipt["response_done"]
            assert "status_code" not in receipt
            assert journal.bytes(
                history(journal, window["key"])[-1]["key"] + ":receipt", receipt["raw_response_blob"]
            )
        monkeypatch.delenv("JEMINI_API_KEY")
        monkeypatch.delenv("OPENAI_API_KEY")
        assert audio_adjudication_cli.main([*common, "resume", *args]) == 0
        assert (
            audio_adjudication_cli.main([*common, "reprocess", *args, "--reason", "Persisted events replay"])
            == 0
        )
        assert len(primary) == 1 and len(secondary) == len(manifest["windows"])
    finally:
        client.close()
    status = json.loads((destination / "status.json").read_text())
    assert status["all_cases_resolved"] and status["active_provider"] == "openai"
    assert (destination / "private/window-1/attempt-2/raw-websocket-events.json").exists()
