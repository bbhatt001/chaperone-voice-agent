"""Stage 2 (output-side) safety model: scope violation detection.

Examines the primary agent's draft response — never the patient's
utterance directly — for diagnosis, prescribing, dose changes, or test
interpretation. Runs in parallel with the grounding check.

Fail-closed: any exception or timeout returns a verdict with violation=True
and error set, so the escalation gate will always escalate on model failure.
"""

import asyncio

from openai import AsyncOpenAI

from voice_agent.config import SAFETY_MODEL, SAFETY_TIMEOUT_SECONDS
from voice_agent.models import ScopeViolationVerdict
from voice_agent.prompt_loader import load_prompt


async def check_scope_violation(client: AsyncOpenAI, draft_response: str) -> ScopeViolationVerdict:
    try:
        completion = await asyncio.wait_for(
            client.chat.completions.parse(
                model=SAFETY_MODEL,
                messages=[
                    {"role": "system", "content": load_prompt("scope_violation_v1.md")},
                    {"role": "user", "content": draft_response},
                ],
                response_format=ScopeViolationVerdict,
            ),
            timeout=SAFETY_TIMEOUT_SECONDS,
        )
        return completion.choices[0].message.parsed
    except Exception as exc:
        # Fail closed: treat any model error or timeout as a scope violation
        # so the escalation gate hands off to a human nurse.
        return ScopeViolationVerdict(
            violation=True,
            violation_type="diagnosis",  # sentinel; `error` holds the real cause
            evidence="",
            reasoning="safety model unavailable — failing closed",
            error=str(exc),
        )
