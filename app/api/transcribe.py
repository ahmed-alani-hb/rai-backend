"""Audio transcription endpoint — uses Groq Whisper for Arabic speech-to-text.

Why server-side: many devices (especially budget Android) lack proper Arabic
voice recognition. Whisper-large-v3 via Groq gives best-in-class Arabic at
near-zero latency on a generous free tier.
"""
from typing import Annotated
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from loguru import logger
from openai import AsyncOpenAI

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import get_settings

router = APIRouter()
settings = get_settings()


@router.post("/audio")
async def transcribe_audio(
    audio: Annotated[UploadFile, File()],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    language: Annotated[str, Form()] = "ar",
):
    """Transcribe an audio file (mp3/m4a/wav/webm/ogg) to Arabic text.

    Uses Groq's Whisper-large-v3 — OpenAI-compatible API, very fast.
    """
    if not settings.groq_api_key:
        raise HTTPException(
            status_code=503,
            detail="GROQ_API_KEY not configured. Get a free key at https://console.groq.com/keys",
        )

    # Read the upload into memory. Whisper accepts files up to 25MB which is
    # ~25 minutes of voice — plenty for any single query.
    contents = await audio.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty audio file")
    if len(contents) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio file too large (max 25MB)")

    logger.info(
        f"transcribe: user={user.username} size={len(contents)} bytes "
        f"filename={audio.filename}"
    )

    # Groq is OpenAI-compatible — point the OpenAI SDK at their endpoint.
    client = AsyncOpenAI(
        api_key=settings.groq_api_key,
        base_url="https://api.groq.com/openai/v1",
    )

    try:
        resp = await client.audio.transcriptions.create(
            model=settings.groq_whisper_model,
            file=(audio.filename or "recording.m4a", contents),
            language=language,
            response_format="json",
            temperature=0.0,
        )
        text = resp.text.strip()
        logger.info(f"transcribe: -> '{text[:100]}'")
        return {"text": text, "language": language}
    except Exception as e:
        logger.exception("Whisper transcription failed")
        msg = str(e)
        if "401" in msg or "auth" in msg.lower():
            raise HTTPException(status_code=503, detail="مفتاح Groq غير صالح")
        if "429" in msg or "rate" in msg.lower():
            raise HTTPException(status_code=429, detail="تجاوز حد التعرّف الصوتي")
        raise HTTPException(status_code=500, detail=f"فشل التعرّف الصوتي: {e}")
