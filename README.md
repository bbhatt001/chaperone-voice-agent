An independent project built to explore safety-first clinical voice agents.

# Chaperone

A turn-based voice agent that makes post-discharge check-in calls — medication
adherence, symptom check, appointment reminders — under the supervision of
independent safety models that can override it.

The name is the thesis. In clinical practice a chaperone is a second person
present during a sensitive encounter, whose only job is to observe. They don't
run the exam. They aren't the clinician's assistant. Their independence is the
point. That is exactly the relationship between the safety models here and the
agent that talks to the patient.

---

## The core claim

**The primary agent does not decide whether to escalate. Independent safety
models do, and the decision is enforced in plain Python.**

A model optimized to be warm and helpful experiences "refuse and escalate" as a
competing objective, and fluency lets it satisfy both badly. The characteristic
failure is not a refusal to acknowledge the symptom — it is a warm
acknowledgement followed by the next scripted question, to a patient who may be
having a cardiac event. Script completion wins because nothing in the model
ranks it below safety.

No prompt engineering fixes that, because the objectives are genuinely in
tension inside a single model. So safety lives outside the conversational model,
in a judge with no stake in the conversation, and the verdict is enforced by
code that cannot be talked out of it.

The same reasoning drives the other two checks. A model asked to be helpful
about medication will drift toward advice; a model asked to be reassuring will
assert things the care plan never said. Both are checked from outside, against
the draft, before anything is spoken.

---

## Architecture

```
Patient audio
    │
    ▼
STT (Deepgram nova-3)
    │
    ▼
patient utterance
    │
    ├──────────────────────────┬─────────────────────────┐
    │                          │                         │
    ▼                          ▼                         │  STAGE 1 (input-side)
[Primary agent LLM]     [Red-flag detector]              │  runs CONCURRENTLY
    │                          │                         │  with the primary agent
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
        │              TTS (Cartesia Sonic)
        │                       │
        └───────────┬───────────┘
                    ▼
        Turn record — printed to stdout on BOTH paths:
        utterance, every safety verdict, the draft,
        the gate's decision, per-stage latency

        ⚠ stdout only. Nothing is persisted to disk yet.
```

### The three safety models

| Stage | Model | Examines | Escalates on |
|---|---|---|---|
| 1 (input) | Red-flag detector | the patient's utterance | chest pain, dyspnea, severe bleeding, stroke signs, suicidal ideation, altered mental status |
| 2 (output) | Scope violation detector | the agent's draft | diagnosis, prescribing, dose change, test interpretation |
| 2 (output) | Grounding check | the agent's draft | clinical claims the care plan doesn't support |

Stage 1 must catch **minimized** symptoms ("it's probably nothing, but…") and
symptoms **buried mid-utterance** among unrelated small talk. Both are the
patterns that get past a cooperative conversational model, which follows the
thread the patient offered rather than the one that matters.

All three return structured JSON, never prose. The prompts are the safety
logic — they live in versioned files under `prompts/`, never inline in Python,
so they can be diffed and reviewed like the code they effectively are.

### The escalation gate

`voice_agent/escalation_gate.py` — one pure function, no I/O, no model calls, no
async. It reads the verdicts and returns a decision. When it fires, the primary
agent's draft is **discarded entirely** and a fixed human-authored script is
spoken instead. The agent does not get to improvise during an emergency.

---

## Fail closed

If a safety model errors, times out, or returns unparseable output → **escalate.**
Errors are checked *before* flags: a model that failed to answer cannot be
trusted to have cleared the turn.

The same rule extends to the audio layer:

| Failure | Behavior | Why |
|---|---|---|
| Safety model error / timeout | **Escalate** | An unverified turn is not a safe turn |
| STT error / timeout | **Escalate** | An utterance we could not hear is an unverified turn |
| Silence, or empty transcript | Re-prompt (3×, then end call politely) | Not a failure. Escalating on a dropped word would make every mumble a nurse page |
| TTS error | Print text, keep the gate's decision | The text already cleared the gate; a speaker outage is a delivery failure, not a safety failure |

**Accepted cost:** transient API failures will escalate healthy patients. That
raises the false-positive rate, and the trade is deliberate — the asymmetry of
harm justifies it. STT gets exactly one retry before failing closed, because
that asymmetry argument justifies escalating on a *failure*, not on a dropped
packet.

The call ends on escalation. Resuming the check-in script after telling a
patient a nurse is coming is the same failure at call level.

---

## Setup

Requires Python 3.12, a microphone, and API keys for OpenAI, Deepgram, and
Cartesia.

```bash
python3.12 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
cp .env.example .env      # then fill in your keys
```

`sounddevice` needs PortAudio, which ships with the macOS/Windows wheels. On
Linux: `apt install libportaudio2`.

### Run it

```bash
./venv/bin/python -m voice_agent.main         # text only — type patient replies
./venv/bin/python -m voice_agent.voice_main   # full voice call
```

The text entrypoint is the one to use while working on safety behavior: same
pipeline, same gate, no audio in the way. The voice entrypoint calibrates your
room's noise floor at startup (stay quiet for a second), then listens.

Two lines worth trying on either:

```
I've been taking everything on schedule, feeling alright.
It's probably nothing, but my chest has been hurting since yesterday.
```

The second one should discard the draft and hand off.

---

## Configuration

All via `.env`. Secrets are never hardcoded; audio keys are read lazily, so the
text pipeline runs without Deepgram or Cartesia credentials at all.

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | — | required |
| `DEEPGRAM_API_KEY` | — | required for voice |
| `CARTESIA_API_KEY` | — | required for voice |
| `PRIMARY_AGENT_MODEL` | `gpt-4o` | talks to the patient; needs conversational quality |
| `SAFETY_MODEL` | `gpt-4o-mini` | classification, not conversation — smaller and faster |
| `SAFETY_TIMEOUT_SECONDS` | `10` | exceeded → escalate |
| `DEEPGRAM_MODEL` | `nova-3` | see the Flux note below |
| `CARTESIA_MODEL` | `sonic-3.5` | the checked-in `.env` overrides this to `sonic-2` |
| `CARTESIA_VOICE` | a voice UUID | accepts a name (`Jacqueline`) or a raw id |
| `MIC_SAMPLE_RATE` | `16000` | what Deepgram wants; 44.1 kHz only buys upload latency |
| `TTS_SAMPLE_RATE` | `24000` | |
| `AUDIO_TIMEOUT_SECONDS` | `8` | per attempt |

**On `DEEPGRAM_MODEL`:** Flux models (`flux-general-en`) are WebSocket-only on
`/v2/listen` and cannot serve this batch path — `check_stt_config()` rejects
them at startup rather than letting it fail mid-call. Flux is a real upgrade
path, not a model-name swap; see [Where this goes next](#where-this-goes-next).

**On voice names:** Cartesia's `q=` search is fuzzy, so a near miss returns a
confident wrong voice. Resolution requires an exact case-insensitive name match
— a typo fails loudly rather than putting a different person's voice on a
patient call.

### Pinned API versions

| Provider | SDK | API version on the wire |
|---|---|---|
| Cartesia | `cartesia==4.0.1` | `cartesia-version: 2026-08-14` |
| Deepgram | `deepgram-sdk==7.7.0` | `/v1/listen` (batch) |
| OpenAI | `openai==2.45.0` | — |

The Cartesia version is **not** configurable — the SDK hardcodes it
(`_client.py:244`), so the version pin travels with the SDK pin in
`requirements.txt`. That makes an SDK bump the thing that changes the API
contract, which on a patient-facing path deserves a deliberate review rather
than a routine dependency upgrade.

---

## Latency

```
turn ≈ STT + max(primary_agent, red_flag) + max(scope, grounding) + TTS
```

Stage 1 is effectively free — it overlaps with the primary agent. Stage 2 is a
tax paid on every non-escalated turn, and it is the bottleneck. Measured:

| Stage | Observed |
|---|---|
| STT (nova-3 batch, pooled connection) | 340–490 ms |
| Pipeline (Stage 1 + Stage 2 + gate) | 2.4–6.1 s |
| TTS (sonic-2) | 1.3–3.4 s |

The safety architecture, not the audio, dominates the turn. Two levers:

- **Early exit** — Stage 1 escalations skip Stage 2 entirely (already built).
- **Incremental Stage 2** — stream the primary agent's tokens and run the output
  checks on partial output, killing the stream on violation. Attacks
  time-to-first-token directly. Documented, not built.

Instrumentation is per-stage and printed every turn. It is a feature, not an
afterthought: you cannot argue about a safety/latency trade you aren't
measuring.

---

## Layout

```
voice_agent/
  main.py              text-only CLI
  voice_main.py        voice CLI + audio failure policy
  pipeline.py          one turn: Stage 1 → Stage 2 → gate
  escalation_gate.py   the decision. plain Python, no LLM
  primary_agent.py     talks to the patient
  models.py            typed verdicts (pydantic), care plan, turn result
  config.py            env loading, model + audio configuration
  prompt_loader.py     versioned prompt files
  safety/
    red_flag_detector.py   Stage 1, input-side
    scope_violation.py     Stage 2, output-side
    grounding_check.py     Stage 2, output-side
  audio/
    devices.py         mic capture w/ silence endpointing, speaker playback
    stt.py             Deepgram
    tts.py             Cartesia
prompts/               the safety logic, versioned
fixtures/              example care plan
```

Naming is deliberate throughout: the **primary agent** talks, **safety models**
judge, the **escalation gate** decides. Never just "the agent."

### Endpointing

Capture decides when the patient has stopped talking (energy threshold + 1.6 s
silence hang, calibrated against the room's noise floor at call start). The hang
is long on purpose: post-discharge patients are often elderly and pause
mid-sentence, and cutting someone off mid-symptom is a safety risk, not a UX
one.

---

## The open question

Safety and warmth are in tension, and this system resolves that tension by
choosing safety every time. Escalate on everything and you have an expensive
switchboard that patients stop answering. Escalate on nothing and you have a
liability.

The honest position: **a miss and a false alarm are not comparable costs, so
they should not be traded at par.** A missed cardiac event is unbounded. A false
alarm costs a nurse five minutes and a patient some anxiety. That asymmetry
justifies a high false-positive rate — but it does not justify an *unmeasured*
one, and it stops justifying anything at all once patients start ignoring the
calls. That is the number to instrument next.

Escalation here is also binary, and that hides a question the system never asks:
nothing in it reasons about *relative* priority within a turn. If a patient
reports a symptom and also asks about their appointment date, no component
decides which one matters more — the gate simply fires or doesn't. Binary
escalation makes that moot. Graded urgency would not, and would be a harder
problem than any single check in this repo.

---

## Where this goes next

- **False-positive rate on benign utterances.** The missing number.
- **Deepgram Flux** (`/v2/listen`, WebSocket) does its own turn detection and
  would delete the endpointing in `devices.py`, cutting STT to roughly zero
  perceived latency. One invariant if it happens: **Stage 1 runs on final
  `EndOfTurn` text only.** Acting on partial `Update` transcripts can clear
  `"Everything's fine, no problems at all"` and never see `"— except my chest
  has been hurting"`. `EagerEndOfTurn` is retractable; a draft is revocable,
  audio already in the patient's ear is not.
- **Incremental Stage 2** on streamed draft tokens.
- **Transcript logging to disk.** Every turn is printed; none of it is persisted.
