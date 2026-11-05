"""Speech-to-text for reels (many outfit videos name the pieces in the voiceover).

Hosted Whisper through the OpenAI-compatible /audio/transcriptions endpoint, so nothing heavy
has to be installed: TRANSCRIBE=groq (free tier, https://console.groq.com/keys) or
TRANSCRIBE=openai, with TRANSCRIBE_API_KEY.
"""

from __future__ import annotations

from pathlib import Path

import httpx

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "whisper-large-v3-turbo"),
    "openai": ("https://api.openai.com/v1", "whisper-1"),
}

# Nudges Whisper towards spelling fashion brand names properly
PROMPT = ("Menswear outfit details: brand names, designers, garments, shoes, fabrics, e.g. "
          "Our Legacy, Kapital, Visvim, Auralee, Engineered Garments, Paraboot, New Balance, Arc'teryx.")


def transcribe(audio: Path, provider: str, api_key: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown transcription provider: {provider}")
    if not api_key:
        raise ValueError(f"TRANSCRIBE={provider} needs TRANSCRIBE_API_KEY")
    base_url, model = PROVIDERS[provider]
    with audio.open("rb") as f:
        resp = httpx.post(
            f"{base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": model, "prompt": PROMPT, "response_format": "json"},
            files={"file": (audio.name, f, "audio/mpeg")},
            timeout=120,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"{provider} transcription error {resp.status_code}: {resp.text[:200]}")
    return resp.json().get("text", "").strip()
