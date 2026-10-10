import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import SessionLocal, get_db
from database.models import Conversation, Message

load_dotenv()

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
RODIUMAI_API_KEY = os.getenv("RODIUMAI_API_KEY")
BASE_URL = os.getenv("BASE_URL", "https://api.rodiumai.io/v1").rstrip("/")
RODIUMAI_URL = f"{BASE_URL}/chat/completions"
ALLOWED_MODELS = tuple(
    dict.fromkeys(
        model.strip()
        for model in os.getenv(
            "ALLOWED_MODELS",
            os.getenv(
                "RODIUMAI_MODELS",
                "anthropic/claude-sonnet-4-5-20250929,openai/gpt-4o-mini",
            ),
        ).split(",")
        if model.strip()
    )
)
if len(ALLOWED_MODELS) < 2:
    raise RuntimeError("ALLOWED_MODELS must configure at least two models.")

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", os.getenv("RODIUMAI_MODEL", ALLOWED_MODELS[0]))
if DEFAULT_MODEL not in ALLOWED_MODELS:
    raise RuntimeError("DEFAULT_MODEL must be included in the configured model allowlist.")

PREVIEW_LENGTH = 60
LLM_ROLES = {"user", "assistant"}
NOTIFICATION_ROLE = "system-notification"
NOTE_ROLE = "note"
NOTIFICATION_EVERY = 10
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."
SYSTEM_PROMPT_PATH = Path(os.getenv("SYSTEM_PROMPT_PATH", "prompts/system.md"))
if not SYSTEM_PROMPT_PATH.is_absolute():
    SYSTEM_PROMPT_PATH = BASE_DIR / SYSTEM_PROMPT_PATH
SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

app = FastAPI(title="Study Buddy Chatbot")
app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend"), name="static")


class ConversationResponse(BaseModel):
    conversation_id: int


class ModelsResponse(BaseModel):
    models: list[str]
    default: str


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None


class ChatRequest(BaseModel):
    conversation_id: int
    message: str = Field(min_length=1)
    model: str = DEFAULT_MODEL


class NoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict[str, str]]:
    """Send only standard dialogue turns; notes and notifications remain UI-only."""
    return [
        {"role": message.role, "content": message.content}
        for message in rows
        if message.role in LLM_ROLES
    ]


def encode_sse(data: str, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {data}\n\n"


def save_completed_turn(
    conversation_id: int,
    next_seq: int,
    history_length: int,
    user_text: str,
    reply: str,
) -> str | None:
    notification = None
    with SessionLocal() as db:
        db.add_all(
            [
                Message(
                    conversation_id=conversation_id,
                    seq=next_seq,
                    role="user",
                    content=user_text,
                ),
                Message(
                    conversation_id=conversation_id,
                    seq=next_seq + 1,
                    role="assistant",
                    content=reply,
                ),
            ]
        )
        if (history_length + 2) % NOTIFICATION_EVERY == 0:
            notification = NOTIFICATION_TEXT
            db.add(
                Message(
                    conversation_id=conversation_id,
                    seq=next_seq + 2,
                    role=NOTIFICATION_ROLE,
                    content=notification,
                )
            )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise RuntimeError("The conversation was updated concurrently.") from exc
    return notification


def stream_error(detail: str) -> str:
    return encode_sse(json.dumps({"detail": detail}), event="error")


async def stream_chat(
    *,
    conversation_id: int,
    next_seq: int,
    history: list[dict[str, str]],
    user_text: str,
    model: str,
) -> AsyncIterator[str]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_text},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 512,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {"Authorization": f"Bearer {RODIUMAI_API_KEY}"}
    reply_parts: list[str] = []
    usage: dict[str, Any] | None = None
    received_done = False

    try:
        timeout = httpx.Timeout(120.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", RODIUMAI_URL, headers=headers, json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        received_done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("The LLM returned an invalid SSE data line.")
                        yield stream_error("The LLM returned an invalid response.")
                        return

                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    if choices:
                        content = choices[0].get("delta", {}).get("content")
                        if isinstance(content, str) and content:
                            reply_parts.append(content)
                            yield encode_sse(json.dumps({"content": content}))

        if not received_done:
            logger.warning("The LLM stream ended before its [DONE] marker.")
            yield stream_error("The LLM stream ended before the response completed.")
            return

        reply = "".join(reply_parts)
        try:
            notification = save_completed_turn(
                conversation_id,
                next_seq,
                len(history),
                user_text,
                reply,
            )
        except RuntimeError:
            logger.exception("Could not save the completed conversation turn.")
            yield stream_error(
                "The response was generated but could not be saved. Please retry."
            )
            return

        if notification:
            yield encode_sse(
                json.dumps({"role": NOTIFICATION_ROLE, "content": notification})
            )
        if usage:
            yield encode_sse(json.dumps(usage), event="usage")
        yield encode_sse("[DONE]")
    except httpx.HTTPError:
        logger.exception("The LLM API stream failed.")
        yield stream_error("The LLM API call failed. Please retry.")
    except Exception:
        logger.exception("Unexpected error while streaming the LLM response.")
        yield stream_error("An unexpected error interrupted the response.")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "frontend" / "index.html")


@app.get("/models")
def list_models() -> ModelsResponse:
    return ModelsResponse(models=list(ALLOWED_MODELS), default=DEFAULT_MODEL)


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: int, db: Session = Depends(get_db)
) -> list[MessageResponse]:
    return [
        MessageResponse(
            seq=message.seq,
            role=message.role,
            content=message.content,
            created_at=message.created_at,
        )
        for message in load_messages(db, conversation_id)
    ]


@app.post("/conversations/{conversation_id}/notes", status_code=201)
def create_note(
    conversation_id: int,
    request: NoteRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    rows = load_messages(db, conversation_id)
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="A note cannot be empty.")
    note = Message(
        conversation_id=conversation_id,
        seq=rows[-1].seq + 1 if rows else 1,
        role=NOTE_ROLE,
        content=content,
    )
    db.add(note)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="The conversation was updated concurrently. Please retry.",
        ) from exc
    db.refresh(note)
    return MessageResponse(
        seq=note.seq,
        role=note.role,
        content=note.content,
        created_at=note.created_at,
    )


@app.post("/chat")
async def chat(
    request: ChatRequest, db: Session = Depends(get_db)
) -> StreamingResponse:
    if request.model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="The requested model is not allowed.")
    if not RODIUMAI_API_KEY:
        raise HTTPException(status_code=503, detail="The LLM API key is not configured.")
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="A message cannot be empty.")

    rows = load_messages(db, request.conversation_id)
    history = build_llm_history(rows)
    next_seq = rows[-1].seq + 1 if rows else 1
    return StreamingResponse(
        stream_chat(
            conversation_id=request.conversation_id,
            next_seq=next_seq,
            history=history,
            user_text=request.message,
            model=request.model,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
