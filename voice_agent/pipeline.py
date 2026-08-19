"""Orchestrates one conversation turn through the two-stage safety
architecture described in CLAUDE.md, then applies the escalation gate.

Stage 1 and the primary agent run concurrently (free latency).
Stage 2 runs only when Stage 1 clears (real added latency).
The escalation gate is plain Python — no LLM gets a vote on escalation.
Safety model errors and timeouts fail closed: they escalate rather than
letting an unverified turn through.
"""

import asyncio

from openai import AsyncOpenAI

from voice_agent.escalation_gate import escalation_gate
from voice_agent.models import ConversationTurn, PatientCarePlan, TurnResult
from voice_agent.primary_agent import generate_draft
from voice_agent.safety.grounding_check import check_grounding
from voice_agent.safety.red_flag_detector import detect_red_flag
from voice_agent.safety.scope_violation import check_scope_violation


async def run_turn(
    client: AsyncOpenAI,
    care_plan: PatientCarePlan,
    history: list[ConversationTurn],
    utterance: str,
) -> TurnResult:
    # Stage 1: primary agent drafts a response while the red-flag detector
    # independently examines the patient's utterance. This overlap is what
    # makes Stage 1 effectively free latency-wise.
    draft_response, red_flag_verdict = await asyncio.gather(
        generate_draft(client, care_plan, history, utterance),
        detect_red_flag(client, utterance),
    )

    if red_flag_verdict.red_flag or red_flag_verdict.error:
        # Early exit: skip Stage 2 entirely. The gate will escalate.
        result = TurnResult(
            utterance=utterance,
            red_flag_verdict=red_flag_verdict,
            draft_response=draft_response,
            stage2_skipped=True,
        )
        return escalation_gate(result)

    # Stage 2: only runs once the draft exists — this is real added latency,
    # paid on every non-escalated turn. The two checks run in parallel with
    # each other.
    scope_violation_verdict, grounding_verdict = await asyncio.gather(
        check_scope_violation(client, draft_response),
        check_grounding(client, draft_response, care_plan),
    )

    result = TurnResult(
        utterance=utterance,
        red_flag_verdict=red_flag_verdict,
        draft_response=draft_response,
        stage2_skipped=False,
        scope_violation_verdict=scope_violation_verdict,
        grounding_verdict=grounding_verdict,
    )
    return escalation_gate(result)
