"""Stage 1 (input-side) safety model: red-flag symptom detection.

Runs concurrently with the primary agent (see CLAUDE.md's latency model),
examining only the patient's utterance — never the agent's draft.

Fail-closed: any exception or timeout returns a verdict with red_flag=True
and error set, so the escalation gate will always escalate on model failure.
"""

import asyncio

from openai import AsyncOpenAI

from voice_agent.config import SAFETY_MODEL, SAFETY_TIMEOUT_SECONDS
from voice_agent.models import RedFlagVerdict
from voice_agent.prompt_loader import load_prompt


async def detect_red_flag(client: AsyncOpenAI, utterance: str) -> RedFlagVerdict:
    try:
        completion = await asyncio.wait_for(
            client.chat.completions.parse(
                model=SAFETY_MODEL,
                messages=[
                    {"role": "system", "content": load_prompt("red_flag_detector_v1.md")},
                    {"role": "user", "content": utterance},
                ],
                response_format=RedFlagVerdict,
            ),
            timeout=SAFETY_TIMEOUT_SECONDS,
        )
        return completion.choices[0].message.parsed
    except Exception as exc:
        # Fail closed: treat any model error or timeout as a red flag so the
        # escalation gate hands off to a human nurse.
        return RedFlagVerdict(
            red_flag=True,
            categories=["other_urgent"],
            evidence="",
            reasoning="safety model unavailable — failing closed",
            error=str(exc),
        )
