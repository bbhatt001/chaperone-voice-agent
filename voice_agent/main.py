"""Text-only CLI driver for the post-discharge check-in pipeline.

No audio (see CLAUDE.md build order, step 4). Every safety verdict and the
gate decision are printed so behavior is fully visible during review.
"""

import asyncio
import json
from pathlib import Path

from voice_agent.config import get_client
from voice_agent.models import ConversationTurn, PatientCarePlan, TurnResult
from voice_agent.pipeline import run_turn

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def load_care_plan(name: str = "care_plan_example.json") -> PatientCarePlan:
    data = json.loads((FIXTURES_DIR / name).read_text())
    return PatientCarePlan.model_validate(data)


def print_turn_result(result: TurnResult) -> None:
    print(f"\n[red-flag verdict]  {result.red_flag_verdict.model_dump_json()}")
    if result.stage2_skipped:
        print("[stage 2]           skipped (red flag or error on input)")
    else:
        print(f"[scope violation]   {result.scope_violation_verdict.model_dump_json()}")
        print(f"[grounding]         {result.grounding_verdict.model_dump_json()}")

    if result.final_action == "escalated":
        print(f"\n*** ESCALATED — {result.escalation_reason} ***")
        print(f"[draft discarded]   {result.draft_response}")
        print(f"\nAgent (script): {result.spoken_text}\n")
    else:
        print(f"\nAgent: {result.spoken_text}\n")


async def main() -> None:
    client = get_client()
    care_plan = load_care_plan()
    history: list[ConversationTurn] = []

    print(f"Post-discharge check-in — patient: {care_plan.patient_name}")
    print("Type patient responses at the prompt. Ctrl-D to end the call.\n")

    while True:
        try:
            utterance = input("Patient: ").strip()
        except EOFError:
            break
        if not utterance:
            continue

        result = await run_turn(client, care_plan, history, utterance)
        print_turn_result(result)

        if result.final_action == "spoken":
            # Only extend history when the agent's draft was actually spoken.
            # Escalated turns hand off to a human nurse; the agent's turn ends.
            history.append(ConversationTurn(role="patient", content=utterance))
            history.append(ConversationTurn(role="agent", content=result.spoken_text))


if __name__ == "__main__":
    asyncio.run(main())
