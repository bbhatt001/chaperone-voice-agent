"""Stage 2 (output-side) safety model: grounding check.

Examines the primary agent's draft response against the patient's care
plan, looking for clinical claims the care plan doesn't support. Runs in
parallel with the scope violation detector.

Fail-closed: any exception or timeout returns a verdict with grounded=False
and error set, so the escalation gate will always escalate on model failure.
"""

import asyncio

from openai import AsyncOpenAI

from voice_agent.config import SAFETY_MODEL, SAFETY_TIMEOUT_SECONDS
from voice_agent.models import GroundingVerdict, PatientCarePlan
from voice_agent.prompt_loader import load_prompt


async def check_grounding(
    client: AsyncOpenAI, draft_response: str, care_plan: PatientCarePlan
) -> GroundingVerdict:
    user_content = (
        f"## Patient care plan\n{care_plan.as_prompt_block()}\n\n"
        f"## Draft response\n{draft_response}"
    )
    try:
        completion = await asyncio.wait_for(
            client.chat.completions.parse(
                model=SAFETY_MODEL,
                messages=[
                    {"role": "system", "content": load_prompt("grounding_check_v1.md")},
                    {"role": "user", "content": user_content},
                ],
                response_format=GroundingVerdict,
            ),
            timeout=SAFETY_TIMEOUT_SECONDS,
        )
        return completion.choices[0].message.parsed
    except Exception as exc:
        # Fail closed: treat any model error or timeout as an ungrounded
        # response so the escalation gate hands off to a human nurse.
        return GroundingVerdict(
            grounded=False,
            unsupported_claims=[],
            reasoning="safety model unavailable — failing closed",
            error=str(exc),
        )
