# Post-Discharge Voice Agent (Safety-First)

## What this is
A turn-based voice agent that performs post-discharge check-in calls with
patients: medication adherence, symptom check, appointment reminders.

Modeled on a safety-constellation architecture: a primary conversational
agent supervised by independent safety models that can override it.

## Core architectural principle
The primary agent does NOT decide whether to escalate.
Independent safety models do, and the decision is enforced in code.

A model optimized to be warm and helpful will experience "refuse and
escalate" as a competing objective, and fluency lets it satisfy both badly.
Safety must therefore live outside the conversational model, in a judge with
no stake in the conversation.

## Hard constraints (non-negotiable)
- The primary agent NEVER diagnoses.
- The primary agent NEVER prescribes or changes a medication dose.
- The primary agent NEVER interprets test results.
- On any red-flag symptom, the call is escalated to a human nurse.
- The escalation gate is plain Python, not an LLM. No model gets a vote.

## Architecture (two-stage safety)

Patient audio
    │
    ▼
STT (Deepgram)
    │
    ▼
patient utterance
    │
    ├──────────────────────────┬─────────────────────────┐
    │                          │                         │
    ▼                          ▼                         │  STAGE 1 (input-side)
[Primary Agent LLM]   [Red-flag detector]                │  runs CONCURRENTLY
    │                          │                         │  with primary agent
    ▼                          ▼                         │  → costs ~0 latency
draft response          input verdict ───────────────────┘
    │                          │
    │                          └──► red flag? ──YES──► ESCALATE
    │                                                  (discard draft; skip
    │                                                   Stage 2 entirely)
    ▼
STAGE 2 (output-side) — runs ONLY if Stage 1 cleared.
Cannot start until the draft exists. This is real added latency.
    │
    ├──► [Scope violation detector]  ─┐  run in parallel
    └──► [Grounding check]           ─┘  with each other
                    │
                    ▼
            ESCALATION GATE (plain Python)
                    │
        ┌───────────┴───────────┐
        │                       │
   any flag raised          all clear
        │                       │
        ▼                       ▼
  hand off to             speak draft response
  human nurse                   │
  (discard draft)               ▼
        │                      TTS
        │                       │
        └───────────┬───────────┘
                    ▼
        Transcript log (ALWAYS — both paths)

## Safety models

STAGE 1 — input-side (examines the PATIENT's utterance):
1. Red-flag detector — chest pain, dyspnea, severe bleeding, stroke signs,
   suicidal ideation, altered mental status → immediate escalation.
   Must catch MINIMIZED symptoms ("it's probably nothing, but...") and
   symptoms buried mid-utterance among unrelated content.

STAGE 2 — output-side (examines the PRIMARY AGENT's draft response):
2. Scope violation detector — is the primary agent about to diagnose,
   prescribe, adjust a dose, or interpret a result?
3. Grounding check — is the primary agent asserting a clinical claim not
   supported by the patient's provided care plan?

## Latency model
turn_latency ≈ STT + max(primary_agent, red_flag) + max(scope, grounding) + TTS

- Stage 1 is effectively free (overlaps with the primary agent).
- Stage 2 is a tax paid on every non-escalated turn. This is the bottleneck.
- Early exit: if Stage 1 escalates, skip Stage 2 entirely.
- Future optimization (document, don't necessarily build): stream the primary
  agent's tokens and run Stage 2 incrementally on partial output, killing the
  stream on violation. Attacks time-to-first-token directly.
- Stage 2 checks are classification, not conversation — use a smaller/faster
  model.

## Fail closed
If a safety model errors, times out, or returns unparseable output → ESCALATE.
Never default to "safe" on failure. An unverified turn is not a safe turn.

Accepted cost: transient API failures will escalate healthy patients. This
raises the false-positive rate. That trade is deliberate — the asymmetry of
harm justifies it.

## Stack
- Python 3.12
- OpenAI API (GPT for primary agent + safety models)
- Deepgram (STT, pre-recorded endpoint — turn-based, not streaming)
- Cartesia Sonic (TTS)
- deepgram-sdk + cartesia SDKs (one client per process, pooled connections)
- sounddevice + numpy for mic capture / speaker playback
- asyncio for concurrency (asyncio.gather)
- Config via .env (OPENAI_API_KEY, DEEPGRAM_API_KEY, CARTESIA_API_KEY);
  never hardcode secrets

Mic PCM goes to Deepgram raw (encoding=linear16 + sample_rate), not wrapped
in a WAV container. Cartesia returns raw PCM for the same reason: no
container needed between a buffer and a sound card.

## Audio failure policy
- STT failure (error, timeout, unparseable) → ESCALATE. An utterance we
  could not hear is an unverified turn, same as a safety model timeout.
- Silence or an empty transcript is NOT a failure → re-prompt, up to 3
  consecutive times, then end the call politely. Escalating on silence
  would make a dropped word a nurse page.
- TTS failure → do NOT escalate. The text already cleared the gate; a
  speaker outage is a delivery failure, not a safety failure. Report the
  degradation, print the text, keep the gate's decision.
- The call ends on escalation. Resuming the check-in script after telling
  the patient a nurse is coming is the script-continues-through-an-emergency
  failure at call level.

## Requirements
- Every turn logged: patient utterance, all safety verdicts, agent draft,
  final action (spoken vs escalated), and per-stage latency.
- Latency instrumentation is a first-class feature, not an afterthought.
- Safety models return STRUCTURED output (JSON), not prose.
- Prompts live in versioned files under prompts/, never inline in Python:
    prompts/primary_agent_v1.md
    prompts/red_flag_detector_v1.md
    prompts/scope_violation_v1.md
    prompts/grounding_check_v1.md
  Prompts ARE the safety logic. Version them, diff them, document why each
  constraint exists.

## Style
- Type hints on all functions.
- Small, testable modules. No god-file.
- Precise naming: "primary agent" (talks), "safety models" (judge),
  "escalation gate" (decides). Never just "the agent."

## Build order (do not skip ahead)
1. Text-only pipeline end to end. No audio at all.
2. Stage 1 + Stage 2 safety models with structured JSON output.
3. Escalation gate + fail-closed error handling.
4. Audio in/out (Whisper + TTS).
5. Latency instrumentation per stage.
6. Adversarial test cases. Document where it FAILS.

## Known open question (be able to defend a position)
Safety and warmth are in tension. Escalate on everything → useless switchboard.
Escalate on nothing → dangerous. Where is the line? What does a miss cost
versus a false alarm?