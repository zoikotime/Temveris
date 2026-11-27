"""
Kairos — in-process FastAPI port of the legacy Node KAIROS-BE chatbot.

Replaces the standalone Express service that lived under KAIROS-BE/. The
current bot is a keyword/intent matcher over `data/knowledge.json`; LLM,
streaming, RAG, and websocket transport are NOT in scope for this module.

Public surface:
    - router (FastAPI APIRouter, mounted at /api/v1/kairos in main.py)
    - ensure_tables() — idempotent table creation, called from main.lifespan
"""

from app.kairos.router import router


def ensure_tables() -> None:
    """Create the four chatbot_* tables on the Kairos engine if missing:

      - chatbot_conversations
      - chatbot_messages
      - chatbot_unknown_prompts
      - chatbot_email_rate_limits

    Uses `app.kairos.database.engine`, which is either the main app
    engine (default) or a dedicated engine when `KAIROS_DATABASE_URL` is
    set — so chatbot tables always land in the Kairos-designated DB.

    For pre-consolidation databases that still have legacy table names
    (`conversations`, `chats`, ...), run
    `python scripts/migrate_kairos_to_chatbot.py` once after the env is
    pointed at the right DB."""
    from app.kairos.database import engine
    from app.kairos.models import (
        KairosChat,
        KairosConversation,
        KairosEmailRateLimit,
        KairosNewPrompt,
    )

    for model in (
        KairosConversation,
        KairosChat,
        KairosNewPrompt,
        KairosEmailRateLimit,
    ):
        model.__table__.create(bind=engine, checkfirst=True)


__all__ = ["router", "ensure_tables"]
