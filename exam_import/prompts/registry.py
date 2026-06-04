from __future__ import annotations


KNOWN_PROMPTS = {
    "step2_layout": "step2_layout.prompt.md",
    "step3_question_json": "step3_question_json.prompt.md",
    "step35_latex_audit": "step35_latex_audit.prompt.md",
    "step4_visual_assets": "step4_visual_assets.prompt.md",
    "step4_answer_tables": "step4_answer_tables.prompt.md",
}


def resolve_prompt_name(name_or_path: str) -> str:
    if name_or_path in KNOWN_PROMPTS:
        return KNOWN_PROMPTS[name_or_path]
    return name_or_path
