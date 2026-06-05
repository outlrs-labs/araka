"""Voice transcription — WhatsApp voice note support via Groq Whisper."""

import logging
import os
import tempfile

from bot.services.whatsapp import download_media
from bot.config import config

logger = logging.getLogger(__name__)


async def transcribe_voice(media_id: str) -> str:
    """Download a WhatsApp voice note and transcribe it via Groq Whisper.

    Args:
        media_id: The WhatsApp media ID from the incoming message payload.

    Returns:
        Transcribed text string, or empty string on failure.
    """
    # Step 1: Download audio bytes via WhatsApp Media API
    audio_bytes = download_media(media_id)
    if not audio_bytes:
        logger.warning(f"No audio bytes returned for media_id={media_id}")
        return ""

    # Step 2: Write to temp file and transcribe
    tmp_path = None
    try:
        from openai import OpenAI
        client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY)

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
