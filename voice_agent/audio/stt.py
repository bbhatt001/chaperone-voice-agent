"""Speech-to-text via Deepgram (pre-recorded / batch endpoint).

Turn-based agent, so this is batch transcription of one complete utterance —
not the streaming socket. The utterance boundary is decided by the capture
layer (silence endpointing in devices.py), not by Deepgram.

This module deliberately does NOT fail closed on its own. It raises, and the
caller decides — because "STT failed" and "the patient said something
dangerous" are different conditions and only the caller knows what the turn
should do about it. See voice_main.py, which escalates on STTError: an
utterance we could not hear is an unverified turn.
"""

import asyncio
from functools import lru_cache

from deepgram import AsyncDeepgramClient

from voice_agent.config import (
    AUDIO_TIMEOUT_SECONDS,
    DEEPGRAM_LANGUAGE,
    DEEPGRAM_MODEL,
    require_env,
)

# One retry. A transient blip (observed once in testing as a read timeout)
# would otherwise escalate a healthy patient to a nurse, and the asymmetry
# argument that justifies failing closed does not justify doing it over a
# dropped packet. More than one retry and the timeout budget stops meaning
# anything on a live call.
STT_MAX_RETRIES = 1


class STTError(RuntimeError):
    """Deepgram was unreachable, errored, timed out, or returned nothing usable."""


def check_stt_config() -> None:
    """Reject STT config this endpoint cannot serve. Call at preflight.

    Deepgram's Flux models are WebSocket-only on /v2/listen — POSTing one to
    the batch endpoint fails, and it would surface as a mid-call escalation
    rather than the configuration error it actually is.
    """
    if DEEPGRAM_MODEL.startswith("flux"):
        raise RuntimeError(
            f"DEEPGRAM_MODEL={DEEPGRAM_MODEL!r} is a Flux model: WebSocket-only on /v2/listen, "
            "not usable from the turn-based batch path. Use nova-3 (or another /v1 model)."
        )


@lru_cache(maxsize=1)
def _client() -> AsyncDeepgramClient:
    """One client per process. Its connection pool is bound to the running
    event loop, which is fine here — one loop, one call."""
    return AsyncDeepgramClient(api_key=require_env("DEEPGRAM_API_KEY"))


async def transcribe(pcm: bytes, sample_rate: int) -> str:
    """Transcribe one captured utterance. Returns "" if Deepgram heard no speech.

    Raises STTError on any transport, status, or parse failure.
    """
    try:
        response = await _client().listen.v1.media.transcribe_file(
            request=pcm,
            model=DEEPGRAM_MODEL,
            language=DEEPGRAM_LANGUAGE,
            # smart_format gives punctuation and number/date formatting. The
            # safety models read this text, so readable output is a safety
            # property, not a cosmetic one: "150 over 90" and "150/90" should
            # not parse differently.
            smart_format=True,
            # Declare the wire format instead of wrapping the mic bytes in a
            # WAV container — devices.py already produces exactly this.
            encoding="linear16",
            request_options={
                # sample_rate and channels are required alongside encoding
                # (raw PCM without them is a 400, "corrupt or unsupported
                # data") but the SDK does not surface them as arguments, so
                # they go through the query-parameter escape hatch. Verified
                # to reach the wire: a bogus value returns "Invalid query
                # string" rather than being silently dropped.
                "additional_query_parameters": {"sample_rate": sample_rate, "channels": 1},
                "timeout": AUDIO_TIMEOUT_SECONDS,
                "max_retries": STT_MAX_RETRIES,
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Broad by design: every failure mode here means the same thing to the
        # caller — we do not know what the patient said.
        raise STTError(f"Deepgram request failed: {type(exc).__name__}: {exc}") from exc

    try:
        transcript = response.results.channels[0].alternatives[0].transcript
    except (AttributeError, IndexError, TypeError) as exc:
        raise STTError(f"unexpected Deepgram response shape: {type(exc).__name__}: {exc}") from exc

    if not isinstance(transcript, str):
        # None or anything else is unparseable, not "the patient said nothing".
        raise STTError(f"transcript was {type(transcript).__name__}, expected str")
    return transcript.strip()
