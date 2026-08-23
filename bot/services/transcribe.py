"""Voice transcription — WhatsApp voice note support via Groq Whisper."""

import asyncio
import logging
import os
import tempfile

from bot.services.whatsapp import download_media
from bot.config import config

logger = logging.getLogger(__name__)

# Whisper on a voice note is slow but bounded; the SDK's own default is 600 s,
# which would pin a message for ten minutes on a hung connection.
_WHISPER_TIMEOUT_S = 60
_WHISPER_MAX_RETRIES = 1


def _transcribe_sync(media_id: str) -> str:
    """Download + transcribe. Blocking by nature — always call via a thread."""
    audio_bytes = download_media(media_id)
    if not audio_bytes:
        logger.warning(f"No audio bytes returned for media_id={media_id}")
        return ""

    tmp_path = None
    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY,
            timeout=_WHISPER_TIMEOUT_S, max_retries=_WHISPER_MAX_RETRIES,
        )

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=("voice.ogg", f, "audio/ogg"),
                response_format="text",
            )

        if isinstance(resp, str):
            return resp.strip()
        return getattr(resp, "text", "").strip()

    except Exception as e:
        logger.error(f"Transcription error for media_id={media_id}: {e}")
        return ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def transcribe_voice(media_id: str) -> str:
    """Download a WhatsApp voice note and transcribe it via Groq Whisper.

    Both steps — two HTTP round trips to Meta, then the Whisper call — are
    synchronous and together took 1.5-4 s. Run on the event loop that froze
    every other user's message for the whole duration, so it goes to a thread.

    Args:
        media_id: The WhatsApp media ID from the incoming message payload.

    Returns:
        Transcribed text string, or empty string on failure.
    """
    return await asyncio.to_thread(_transcribe_sync, media_id)
