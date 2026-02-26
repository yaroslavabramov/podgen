import asyncio
import os
import pytest

# Tests must never emit production LangSmith traces.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session", autouse=True)
def disable_langsmith_tracing_for_tests() -> None:
    from app.config import settings

    settings.langsmith_tracing = False
    settings.langsmith_sampling_rate = 0.0
    # Tests should not write extractor debug dumps to project-local folders
    # even if WEB_EXTRACTOR_DEBUG_DUMP_DIR is set in .env.
    settings.web_extractor_debug_dump_dir = ""
