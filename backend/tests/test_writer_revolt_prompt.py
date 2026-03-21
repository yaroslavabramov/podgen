from app.pipeline.phase6_writing.writer_revolt import EVAL_SYSTEM


def test_writer_revolt_prompt_tracks_style_rigidity_and_scene_echo() -> None:
    assert "rigid_turn_taking" in EVAL_SYSTEM
    assert "repetitive_agreement_shape" in EVAL_SYSTEM
    assert "repetitive_challenge_shape" in EVAL_SYSTEM
    assert "scene_opening_echo" in EVAL_SYSTEM
    assert "teacherly_exposition" in EVAL_SYSTEM
    assert "scene-to-scene echo where a scene opens by repeating the previous scene's punchline" in EVAL_SYSTEM
    assert "rigid turn-taking that makes the exchange feel mechanically alternating" in EVAL_SYSTEM
