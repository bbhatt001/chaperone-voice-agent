"""Microphone capture and speaker playback, via PortAudio (sounddevice).

Capture does its own endpointing: turn-based agent, one utterance per turn, so
this layer answers exactly one question — "has the patient stopped talking?"
That lives here rather than in the STT provider, which keeps the policy visible
and tunable in code. (A streaming model with built-in turn detection, e.g.
Deepgram Flux, would take this job over; see CLAUDE.md's audio notes.)

Endpointing is deliberately patient: post-discharge patients are often elderly,
frequently pause mid-sentence, and cutting them off mid-symptom is a safety
risk, not just a UX one. SILENCE_HANG_SECONDS is set long for that reason.

Playback is blocking — a turn-based agent must not start listening while it is
still talking, or it transcribes itself. (A streaming agent solves this the
other way, by muting the mic while speaking, and gains barge-in for it.)

Everything here blocks on the audio device; call via asyncio.to_thread so the
event loop stays free.
"""

from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from voice_agent.config import MIC_SAMPLE_RATE, TTS_SAMPLE_RATE

FRAME_MS = 30
FRAME_SAMPLES = MIC_SAMPLE_RATE * FRAME_MS // 1000
SAMPLE_WIDTH = 2  # int16

# How long the patient may pause mid-utterance before we consider the turn
# over. Long on purpose — see module docstring.
SILENCE_HANG_SECONDS = 1.6
# Hard cap on one utterance, so a stuck-open mic cannot hang the call.
MAX_UTTERANCE_SECONDS = 45.0
# How long to wait for the patient to start speaking at all.
SPEECH_START_TIMEOUT_SECONDS = 20.0
# A frame must exceed noise_floor * this to count as speech.
SPEECH_THRESHOLD_MULTIPLIER = 3.0
# Absolute floor, so a dead-silent room does not make the threshold ~0 and
# turn every rustle into speech.
MIN_SPEECH_RMS = 180.0


@dataclass(frozen=True)
class CaptureResult:
    """One capture attempt. `pcm` is empty when nothing was heard."""

    pcm: bytes
    heard_speech: bool
    timed_out: bool  # patient never started speaking

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm) / (MIC_SAMPLE_RATE * SAMPLE_WIDTH)


def _rms(frame: bytes) -> float:
    """Root-mean-square level of one int16 frame.

    numpy rather than stdlib audioop: audioop is removed in Python 3.13.
    """
    samples = np.frombuffer(frame, dtype="<i2").astype(np.float32)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


def calibrate_noise_floor(seconds: float = 1.0) -> float:
    """Measure ambient RMS so the speech threshold adapts to the room.

    Run once at the start of a call, while the line is quiet.
    """
    frames = max(1, int(seconds * 1000 / FRAME_MS))
    levels: list[float] = []
    with sd.RawInputStream(
        samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES
    ) as stream:
        for _ in range(frames):
            data, _overflowed = stream.read(FRAME_SAMPLES)
            levels.append(_rms(bytes(data)))
    return sum(levels) / len(levels)


def speech_threshold(noise_floor: float) -> float:
    return max(noise_floor * SPEECH_THRESHOLD_MULTIPLIER, MIN_SPEECH_RMS)


def record_utterance(threshold: float) -> CaptureResult:
    """Record until the patient stops speaking. Blocks; run in a thread.

    Waits up to SPEECH_START_TIMEOUT_SECONDS for speech to begin, then records
    until SILENCE_HANG_SECONDS of sub-threshold audio or MAX_UTTERANCE_SECONDS,
    whichever comes first.
    """
    hang_frames = int(SILENCE_HANG_SECONDS * 1000 / FRAME_MS)
    max_frames = int(MAX_UTTERANCE_SECONDS * 1000 / FRAME_MS)
    start_timeout_frames = int(SPEECH_START_TIMEOUT_SECONDS * 1000 / FRAME_MS)

    chunks: list[bytes] = []
    # Frames captured before speech began, kept so the utterance does not start
    # clipped — the first syllable is often the quietest part of it.
    preroll: list[bytes] = []
    preroll_frames = max(1, hang_frames // 4)

    heard_speech = False
    silent_run = 0
    waited = 0

    with sd.RawInputStream(
        samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES
    ) as stream:
        while True:
            data, _overflowed = stream.read(FRAME_SAMPLES)
            frame = bytes(data)
            is_speech = _rms(frame) >= threshold

            if not heard_speech:
                if is_speech:
                    heard_speech = True
                    chunks.extend(preroll)
                    chunks.append(frame)
                    continue
                preroll.append(frame)
                if len(preroll) > preroll_frames:
                    del preroll[0]
                waited += 1
                if waited >= start_timeout_frames:
                    return CaptureResult(pcm=b"", heard_speech=False, timed_out=True)
                continue

            chunks.append(frame)
            silent_run = 0 if is_speech else silent_run + 1
            if silent_run >= hang_frames or len(chunks) >= max_frames:
                break

    return CaptureResult(pcm=b"".join(chunks), heard_speech=True, timed_out=False)


def play_pcm(pcm: bytes, sample_rate: int = TTS_SAMPLE_RATE) -> None:
    """Play raw signed 16-bit little-endian PCM and wait for it to finish."""
    if not pcm:
        return
    sd.play(np.frombuffer(pcm, dtype="<i2"), samplerate=sample_rate, blocking=True)
