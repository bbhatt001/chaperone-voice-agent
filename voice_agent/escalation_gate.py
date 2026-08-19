"""Escalation gate: plain Python decision that acts on safety verdicts.

This is the only place in the pipeline that decides whether to speak the
draft or hand off to a human nurse. No LLM gets a vote here.

Design:
- Fail closed: errors are checked BEFORE flags. Any safety model error or
  timeout → escalate, regardless of what the flag fields say.
- Fixed script: when the gate fires, the primary agent's draft is discarded
  entirely and ESCALATION_SCRIPT is spoken instead. The agent must not
  improvise during an escalation.
- One function, pure Python. No async, no I/O, no model calls.
"""

from voice_agent.models import TurnResult

# Fixed script spoken on every escalation — no model involvement, no
# improvisation. Content is deliberately minimal: acknowledge, reassure,
# hand off. Any content change requires a new versioned review.
ESCALATION_SCRIPT = (
    "I'm going to connect you with one of our nurses right away. "
    "Please stay on the line — someone will be with you shortly."
)


def escalation_gate(result: TurnResult) -> TurnResult:
    """Apply the escalation gate and return an updated TurnResult.

    Sets final_action to "escalated" or "spoken", records the reason, and
    sets spoken_text to either ESCALATION_SCRIPT or the draft. The primary
    agent's draft is discarded (not spoken) on any escalation.
    """
    reason = _escalation_reason(result)
    if reason:
        return result.model_copy(
            update={
                "final_action": "escalated",
                "escalation_reason": reason,
                "spoken_text": ESCALATION_SCRIPT,
            }
        )
    return result.model_copy(
        update={
            "final_action": "spoken",
            "spoken_text": result.draft_response,
        }
    )


def _escalation_reason(result: TurnResult) -> str | None:
    """Return a human-readable reason if escalation is warranted, else None.

    Errors are checked first (fail-closed): a model that failed to answer
    cannot be trusted to have cleared the turn.
    """
    # --- Fail-closed: safety model errors always escalate first ---
    if result.red_flag_verdict.error:
        return f"safety model error (red-flag detector): {result.red_flag_verdict.error}"

    if result.scope_violation_verdict and result.scope_violation_verdict.error:
        return f"safety model error (scope violation): {result.scope_violation_verdict.error}"

    if result.grounding_verdict and result.grounding_verdict.error:
        return f"safety model error (grounding check): {result.grounding_verdict.error}"

    # --- Stage 1: red flag on the patient's utterance ---
    if result.red_flag_verdict.red_flag:
        categories = ", ".join(result.red_flag_verdict.categories)
        return f"red flag detected: {categories}"

    # --- Stage 2: violations in the draft (only present when stage 2 ran) ---
    if result.scope_violation_verdict and result.scope_violation_verdict.violation:
        return f"scope violation: {result.scope_violation_verdict.violation_type}"

    if result.grounding_verdict and not result.grounding_verdict.grounded:
        if result.grounding_verdict.unsupported_claims:
            claims = "; ".join(result.grounding_verdict.unsupported_claims)
            return f"grounding failure: {claims}"
        return "grounding failure: draft contains unsupported clinical claims"

    return None
