from .providers.elevenlabs import ElevenLabs
from .providers.fish import Fish
from .providers.gemini import Gemini
from .providers.inworld import Inworld
from .providers.minimax import MiniMax

ADAPTERS = dict(elevenlabs=ElevenLabs, gemini=Gemini, inworld=Inworld, fish=Fish, minimax=MiniMax)


def adapter(provider):
    return ADAPTERS[provider]()
