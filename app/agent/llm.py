"""The single place that picks the chat model."""

from langchain_core.language_models import BaseChatModel

from app.config import get_settings


def get_llm() -> BaseChatModel:
    """Return the configured chat model. Swap providers here only."""
    s = get_settings()
    if s.llm_provider != "gemini":
        raise ValueError(f"Unsupported LLM_PROVIDER: {s.llm_provider}")
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model=s.llm_model, google_api_key=s.google_api_key, temperature=0)
