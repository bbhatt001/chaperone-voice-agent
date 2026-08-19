"""Environment loading, OpenAI client factory, and model configuration.

Audio provider keys are read lazily via require_env() rather than at import
time, so the text-only pipeline (build order step 1-3) still runs on a machine
with no Deepgram or Cartesia credentials.
"""

import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# The primary agent talks to the patient — needs conversational quality.
PRIMARY_AGENT_MODEL = os.getenv("PRIMARY_AGENT_MODEL", "gpt-4o")

# Safety models classify, they don't converse — use a smaller/faster model
# so Stage 2 latency stays low (see CLAUDE.md's latency model).
SAFETY_MODEL = os.getenv("SAFETY_MODEL", "gpt-4o-mini")


# Hard timeout for each individual safety model call. Exceeded → escalate.
# See CLAUDE.md: "any safety model error or timeout also escalates."
SAFETY_TIMEOUT_SECONDS: float = float(os.getenv("SAFETY_TIMEOUT_SECONDS", "10"))


def get_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=OPENAI_API_KEY)


# --- Audio layer (build order step 4) ---------------------------------------

# STT: Deepgram pre-recorded transcription of one captured utterance.
# nova-3, not flux-*: Flux is WebSocket-only on /v2/listen (POST there is a
# 405). Flux does its own turn detection, which would replace the endpointing
# in audio/capture.py — a real upgrade path, but a streaming rewrite, not a
# model-name swap. stt.check_stt_config() rejects flux-* rather than letting
# it fail mid-call.
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3")
DEEPGRAM_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "en")

# TTS: Cartesia Sonic.
CARTESIA_MODEL = os.getenv("CARTESIA_MODEL", "sonic-3.5")
# Voice name (e.g. "Jacqueline") or raw voice UUID — names are resolved once
# per process against Cartesia's /voices listing.
CARTESIA_VOICE = os.getenv("CARTESIA_VOICE", "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4")
# Pinned, not floating: a Cartesia API version bump can change response shape,
# and that must be an explicit, reviewed change on a patient-facing path.
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-08-14")

# 16 kHz mono is what Deepgram wants and is plenty for telephony-grade speech;
# sending 44.1 kHz just buys upload latency.
MIC_SAMPLE_RATE: int = int(os.getenv("MIC_SAMPLE_RATE", "16000"))
TTS_SAMPLE_RATE: int = int(os.getenv("TTS_SAMPLE_RATE", "24000"))

# Hard timeout per STT/TTS attempt. STT exceeding it (after its one retry)
# escalates the turn — an utterance we could not hear is an unverified turn.
# Measured round trips are well under 2s, so this is generous margin; keep it
# short enough that the worst case (timeout + retry) is still a tolerable
# silence on a live call.
AUDIO_TIMEOUT_SECONDS: float = float(os.getenv("AUDIO_TIMEOUT_SECONDS", "8"))


def require_env(name: str) -> str:
    """Fetch a required credential at point of use, with an actionable error."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set — add it to .env to use the audio pipeline")
    return value
