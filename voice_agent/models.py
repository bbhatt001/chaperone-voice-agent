"""Typed data structures shared across the pipeline.

Safety-model verdicts are Pydantic models so their outputs are enforced as
structured JSON, never prose (see CLAUDE.md: "Safety models return STRUCTURED
output (JSON), not prose").
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

RedFlagCategory = Literal[
    "chest_pain",
    "dyspnea",
    "severe_bleeding",
    "stroke_signs",
    "suicidal_ideation",
    "altered_mental_status",
    "other_urgent",
    "none",
]

ScopeViolationType = Literal[
    "diagnosis",
    "prescription_or_dose_change",
    "test_interpretation",
    "none",
]


class PatientCarePlan(BaseModel):
    patient_name: str
    medications: list[str]
    discharge_instructions: list[str]
    next_appointment: str
    condition_summary: str

    def as_prompt_block(self) -> str:
        """Renders the care plan as context text for prompts (primary agent
        and grounding check both need this — kept in one place)."""
        return (
            f"Patient: {self.patient_name}\n"
            f"Medications: {'; '.join(self.medications)}\n"
            f"Discharge instructions: {'; '.join(self.discharge_instructions)}\n"
            f"Next appointment: {self.next_appointment}\n"
            f"Condition summary: {self.condition_summary}"
        )


class ConversationTurn(BaseModel):
    role: Literal["patient", "agent"]
    content: str


class RedFlagVerdict(BaseModel):
    red_flag: bool = Field(description="True if the utterance contains any red-flag symptom.")
    categories: list[RedFlagCategory] = Field(
        description=(
            'All red-flag categories present in the utterance, not just the most '
            'severe one — symptom co-occurrence is clinically meaningful. ["none"] '
            "if the utterance is clear."
        )
    )
    evidence: str = Field(description="Quote or close paraphrase of the concerning part of the utterance.")
    reasoning: str
    error: str | None = None

    @model_validator(mode="after")
    def _categories_consistent(self) -> "RedFlagVerdict":
        is_clear = self.categories == ["none"]
        if is_clear and self.red_flag:
            raise ValueError('red_flag=True but categories is ["none"]')
        if not is_clear and not self.red_flag:
            raise ValueError("red_flag=False but categories contains a non-'none' entry")
        if "none" in self.categories and len(self.categories) > 1:
            raise ValueError('"none" cannot be combined with other categories')
        return self


class ScopeViolationVerdict(BaseModel):
    violation: bool = Field(description="True if the draft diagnoses, prescribes, changes a dose, or interprets a test result.")
    violation_type: ScopeViolationType
    evidence: str = Field(description="Quote or close paraphrase of the offending part of the draft.")
    reasoning: str
    error: str | None = None


class GroundingVerdict(BaseModel):
    grounded: bool = Field(description="True if every clinical claim in the draft is supported by the patient's care plan.")
    unsupported_claims: list[str] = Field(default_factory=list)
    reasoning: str
    error: str | None = None


class TurnResult(BaseModel):
    utterance: str
    red_flag_verdict: RedFlagVerdict
    draft_response: str
    stage2_skipped: bool
    scope_violation_verdict: ScopeViolationVerdict | None = None
    grounding_verdict: GroundingVerdict | None = None
    # Set by the escalation gate (step 3). None until the gate has run.
    final_action: Literal["spoken", "escalated"] | None = None
    escalation_reason: str | None = None
    # What was actually output: draft_response if spoken, ESCALATION_SCRIPT if escalated.
    spoken_text: str | None = None
