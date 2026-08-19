"""Text-to-speech via Cartesia Sonic.

Returns raw PCM rather than a WAV container on purpose: it goes straight to the
output device, and Cartesia's WAV header carries a placeholder RIFF length
(0xFFFFFFFF) for streamed output that strict parsers reject anyway.

Like stt.py, this raises instead of failing closed. TTS failure is a delivery
failure, not a safety failure — the text it was handed has already cleared the
escalation gate — so the caller degrades to printing rather than escalating.
See voice_main.py.

API version: `cartesia-version: 2026-08-14`, sent by cartesia==4.0.1 (hardcoded
at _client.py:244). It is NOT configurable from here — the version pin now
travels with the SDK pin in requirements.txt, so bumping the SDK is what changes
the API contract. On a patient-facing path that bump wants a deliberate review,
not a routine dependency upgrade.
"""

import asyncio
import re
from functools import lru_cache

from cartesia import AsyncCartesia

from voice_agent.config import (
    AUDIO_TIMEOUT_SECONDS,
    CARTESIA_MODEL,
    CARTESIA_VOICE,
    TTS_SAMPLE_RATE,
    require_env,
)

_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
VOICE_SEARCH_LIMIT = 20

_voice_id_cache: dict[str, str] = {}


class TTSError(RuntimeError):
    """Cartesia was unreachable, errored, timed out, or returned no audio."""


@lru_cache(maxsize=1)
def _client() -> AsyncCartesia:
    return AsyncCartesia(api_key=require_env("CARTESIA_API_KEY"))


async def resolve_voice_id(voice: str = CARTESIA_VOICE) -> str:
    """Map a configured voice to a Cartesia voice id.

    Accepts a raw UUID (returned unchanged) or a voice name, which is looked up
    once per process. Names are what a human puts in .env; the API only takes
    ids. Resolve at preflight so a typo'd voice name is a startup error rather
    than a mid-call failure.
    """
    if _UUID_PATTERN.match(voice):
        return voice
    if voice in _voice_id_cache:
        return _voice_id_cache[voice]

    try:
        # Server-side search rather than walking every page — the account has
        # hundreds of voices.
        page = await _client().voices.list(q=voice, limit=VOICE_SEARCH_LIMIT)
        matches = list(page.data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise TTSError(f"could not list Cartesia voices: {type(exc).__name__}: {exc}") from exc

    # q is a fuzzy search, so it answers a near-miss with a confident wrong
    # voice. Require an exact name match: a typo should fail loudly, not put a
    # different person's voice on a patient call.
    for candidate in matches:
        if (candidate.name or "").casefold() == voice.casefold():
            _voice_id_cache[voice] = candidate.id
            return candidate.id

    near = ", ".join(c.name for c in matches[:5]) or "nothing"
    raise TTSError(f"no Cartesia voice named exactly {voice!r} — search returned {near}")


async def synthesize(text: str) -> bytes:
    """Render text to raw signed 16-bit little-endian PCM at TTS_SAMPLE_RATE.

    Raises TTSError on any transport or status failure.
    """
    voice_id = await resolve_voice_id()
    try:
        response = await _client().tts.generate(
            model_id=CARTESIA_MODEL,
            transcript=text,
            voice={"id": voice_id},
            output_format={
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": TTS_SAMPLE_RATE,
            },
            language="en",
            timeout=AUDIO_TIMEOUT_SECONDS,
        )
        audio = b"".join([chunk async for chunk in response.iter_bytes()])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise TTSError(f"Cartesia request failed: {type(exc).__name__}: {exc}") from exc

    if not audio:
        raise TTSError("Cartesia returned an empty audio body")
    return audio


async def aclose() -> None:
    """Release the Cartesia connection pool at end of call."""
    if _client.cache_info().currsize:
        await _client().close()
        _client.cache_clear()
