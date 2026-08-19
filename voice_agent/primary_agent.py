"""The primary agent — talks to the patient. Never diagnoses, prescribes,
changes a dose, or interprets a test result (enforced by Stage 2 safety
models, not by this module — see CLAUDE.md's core architectural principle).
"""

from openai import AsyncOpenAI

from voice_agent.config import PRIMARY_AGENT_MODEL
from voice_agent.models import ConversationTurn, PatientCarePlan
from voice_agent.prompt_loader import load_prompt

_ROLE_MAP: dict[str, str] = {"patient": "user", "agent": "assistant"}


async def generate_draft(
    client: AsyncOpenAI,
    care_plan: PatientCarePlan,
    history: list[ConversationTurn],
    utterance: str,
) -> str:
    system_prompt = (
        f"{load_prompt('primary_agent_v1.md')}\n\n## Patient care plan\n{care_plan.as_prompt_block()}"
    )
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": _ROLE_MAP[turn.role], "content": turn.content} for turn in history]
    messages.append({"role": "user", "content": utterance})

    completion = await client.chat.completions.create(
        model=PRIMARY_AGENT_MODEL,
        messages=messages,
    )
    return completion.choices[0].message.content or ""
