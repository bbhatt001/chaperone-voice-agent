"""Voice CLI driver — the same pipeline as main.py, with audio on both ends.

    mic → Deepgram STT → [pipeline: Stage 1 / Stage 2 / escalation gate]
        → Cartesia TTS → speakers

The safety architecture is untouched by this module. Audio is strictly an I/O
skin: nothing here decides what is spoken, it only carries what the escalation
gate already decided.

Two audio-specific policies, both deliberate:

1. STT failure escalates. If Deepgram errors or times out we do not know what
   the patient said, and an utterance we could not hear is an unverified turn.
   Same fail-closed logic as a safety model timeout (CLAUDE.md, "Fail closed").
   Silence is NOT a failure — an empty transcript re-prompts instead.

2. TTS failure does not escalate. By the time we synthesize, the text has
   already cleared the gate; Cartesia being down is a delivery problem, not a
   safety one. The turn is reported as spoken-but-undelivered and the text is
   printed, since escalating here would hand a healthy patient to a nurse over
   a speaker outage.

The call ends on escalation. The agent has just told the patient a nurse is
coming; resuming the check-in script after that is failure F1 (FAILURES.md)
replayed at call level.
"""

import asyncio
import time

from voice_agent.audio.devices import (
    CaptureResult,
    calibrate_noise_floor,
    play_pcm,
    record_utterance,
    speech_threshold,
)
from voice_agent.audio.stt import STTError, check_stt_config, transcribe
from voice_agent.audio.tts import TTSError, aclose, resolve_voice_id, synthesize
from voice_agent.config import (
    CARTESIA_MODEL,
    CARTESIA_VOICE,
    DEEPGRAM_MODEL,
    MIC_SAMPLE_RATE,
    get_client,
    require_env,
)
from voice_agent.escalation_gate import ESCALATION_SCRIPT
from voice_agent.main import load_care_plan, print_turn_result
from voice_agent.models import ConversationTurn, PatientCarePlan
from voice_agent.pipeline import run_turn

# Fixed, human-authored scripts. No model writes these, so they need no
# safety check — same rule as ESCALATION_SCRIPT.
OPENING_SCRIPT = (
    "Hello {name}, this is the automated check-in call from your care team. "
    "I have a few quick questions about how you've been doing since you got home. "
    "How are you feeling today?"
)
REPROMPT_SCRIPT = "Sorry, I didn't catch that. Could you say it again?"
NO_ANSWER_SCRIPT = "I'm not hearing anything, so I'll end the call here. Please call the clinic if you need us."

# Consecutive unusable captures (silence or empty transcript) before giving up.
MAX_EMPTY_TURNS = 3


async def speak(text: str) -> float | None:
    """Synthesize and play `text`. Returns TTS latency in ms, or None on failure.

    Never raises: a speaker failure must not take down a call that the gate has
    already ruled on. The caller reports the degradation.
    """
    started = time.perf_counter()
    try:
        pcm = await synthesize(text)
    except TTSError as exc:
        print(f"[tts FAILED]        {exc}")
        print("[tts fallback]      text only (turn decision stands)")
        return None
    elapsed_ms = (time.perf_counter() - started) * 1000
    await asyncio.to_thread(play_pcm, pcm)
    return elapsed_ms


async def listen(threshold: float) -> CaptureResult:
    print("\n[listening...]")
    capture = await asyncio.to_thread(record_utterance, threshold)
    if capture.heard_speech:
        print(f"[captured]          {capture.duration_seconds:.1f}s")
    return capture


async def run_call(care_plan: PatientCarePlan) -> None:
    # Preflight: bad credentials, an unusable STT model, or an unknown voice
    # name are config errors, not call-time failures. Fail before the patient
    # hears anything, rather than escalating mid-call on the first turn.
    require_env("DEEPGRAM_API_KEY")
    require_env("CARTESIA_API_KEY")
    check_stt_config()
    voice_id = await resolve_voice_id()

    client = get_client()
    history: list[ConversationTurn] = []

    print(f"Post-discharge check-in (voice) — patient: {care_plan.patient_name}")
    print(f"[stt] {DEEPGRAM_MODEL}   [tts] {CARTESIA_MODEL} / {CARTESIA_VOICE} ({voice_id})")
    print("[calibrating mic — stay quiet for a second]")
    threshold = speech_threshold(await asyncio.to_thread(calibrate_noise_floor))
    print(f"[speech threshold]  rms {threshold:.0f}")

    opening = OPENING_SCRIPT.format(name=care_plan.patient_name.split()[0])
    print(f"\nAgent: {opening}")
    await speak(opening)

    empty_turns = 0
    while True:
        turn_started = time.perf_counter()
        capture = await listen(threshold)

        if not capture.heard_speech:
            empty_turns += 1
            if empty_turns >= MAX_EMPTY_TURNS:
                print(f"\nAgent: {NO_ANSWER_SCRIPT}")
                await speak(NO_ANSWER_SCRIPT)
                return
            await speak(REPROMPT_SCRIPT)
            continue

        stt_started = time.perf_counter()
        try:
            utterance = await transcribe(capture.pcm, MIC_SAMPLE_RATE)
        except STTError as exc:
            # Fail closed — we do not know what the patient said.
            print(f"\n[stt FAILED]        {exc}")
            print(f"*** ESCALATED — speech-to-text failure: {exc} ***")
            print(f"\nAgent (script): {ESCALATION_SCRIPT}\n")
            await speak(ESCALATION_SCRIPT)
            return
        stt_ms = (time.perf_counter() - stt_started) * 1000

        if not utterance:
            # Deepgram heard audio but no words. Not a failure — do not escalate.
            empty_turns += 1
            print(f"[stt]               (no speech recognized, {stt_ms:.0f}ms)")
            if empty_turns >= MAX_EMPTY_TURNS:
                print(f"\nAgent: {NO_ANSWER_SCRIPT}")
                await speak(NO_ANSWER_SCRIPT)
                return
            await speak(REPROMPT_SCRIPT)
            continue

        empty_turns = 0
        print(f"\nPatient: {utterance}")

        pipeline_started = time.perf_counter()
        result = await run_turn(client, care_plan, history, utterance)
        pipeline_ms = (time.perf_counter() - pipeline_started) * 1000

        print_turn_result(result)
        tts_ms = await speak(result.spoken_text)

        tts_label = f"{tts_ms:.0f}ms" if tts_ms is not None else "FAILED"
        total_ms = (time.perf_counter() - turn_started) * 1000
        print(
            f"[latency]           stt {stt_ms:.0f}ms | pipeline {pipeline_ms:.0f}ms | "
            f"tts {tts_label} | turn {total_ms:.0f}ms (incl. {capture.duration_seconds * 1000:.0f}ms speaking)"
        )

        if result.final_action == "escalated":
            print("\n[call handed off to human nurse — agent stops here]")
            return

        history.append(ConversationTurn(role="patient", content=utterance))
        history.append(ConversationTurn(role="agent", content=result.spoken_text))


async def main() -> None:
    try:
        await run_call(load_care_plan())
    except KeyboardInterrupt:
        print("\n[call ended]")
    finally:
        await aclose()


if __name__ == "__main__":
    asyncio.run(main())
