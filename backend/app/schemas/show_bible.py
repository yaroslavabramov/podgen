import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class HostPersona(BaseModel):
    name: str
    role: str = Field(..., pattern=r"^(Guide|Challenger)$")
    vocabulary_rules: str = ""
    interaction_style: str = ""
    explanation_behavior: str = ""
    constraints: list[str] = []


class VoiceProfile(BaseModel):
    host_name: str
    tts_provider: str = "elevenlabs"
    voice_id: str
    settings: dict = Field(default_factory=lambda: {
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.3,
        "speed": 1.0,
    })


class AudienceProfile(BaseModel):
    level: str = Field(..., pattern=r"^(beginner|casual|professional)$")
    knows: list[str] = []
    explain: list[str] = []


class FormatRules(BaseModel):
    max_reply_tokens: int = 100
    summary_every_n_scenes: int = 3
    humor_level: str = "light"
    profanity_allowed: bool = False


class ShowBibleCreate(BaseModel):
    audience_profile: AudienceProfile | None = None
    tone_and_mood: str = "conversational"
    show_style: str = "Conversational"
    hosts: list[HostPersona] = []
    voice_profiles: list[VoiceProfile] = []
    forbidden_phrases: list[str] = []
    rigor_policy: str | None = None
    adjacency_policy: str | None = None
    explanation_policy: str | None = None
    format_rules: FormatRules | None = None
    is_preset: bool = False
    preset_name: str | None = None


class ShowBibleUpdate(BaseModel):
    audience_profile: AudienceProfile | None = None
    tone_and_mood: str | None = None
    show_style: str | None = None
    hosts: list[HostPersona] | None = None
    voice_profiles: list[VoiceProfile] | None = None
    forbidden_phrases: list[str] | None = None
    rigor_policy: str | None = None
    adjacency_policy: str | None = None
    explanation_policy: str | None = None
    format_rules: FormatRules | None = None


class ShowBibleResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    audience_profile: dict
    tone_and_mood: str
    show_style: str
    hosts: list
    voice_profiles: list
    forbidden_phrases: list[str]
    rigor_policy: str | None
    adjacency_policy: str | None
    explanation_policy: str | None
    format_rules: dict
    is_preset: bool
    preset_name: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ShowBibleGenerateRequest(BaseModel):
    topic: str
    audience_level: str = Field(default="casual", pattern=r"^(beginner|casual|professional)$")
    show_style: str = Field(default="Conversational")
