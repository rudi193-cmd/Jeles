"""compiler.py — render the canonical Jeles persona JSON into a system prompt.

Ported from utety-chat's persona_compiler.py (the canonical renderer) so the
`jeles` organ is self-sufficient: the structured JSON is the single source of
truth, and this turns it into the labeled prompt string hosts feed an LLM. The
combine (#18) means every consumer renders from ONE json here instead of
carrying its own hand-authored prose copy.

Stdlib only. Deterministic: same JSON in, same prompt out.
"""

from __future__ import annotations


def _append_closing_discipline(parts: list, discipline) -> None:
    if not discipline:
        return
    if isinstance(discipline, str):
        parts.append("CLOSING DISCIPLINE:\n" + discipline.strip())
        return
    if isinstance(discipline, list):
        lines = [str(x).strip() for x in discipline if str(x).strip()]
        if lines:
            parts.append("CLOSING DISCIPLINE:\n" + "\n".join(f"- {line}" for line in lines))


def compile_persona(data: dict) -> str:
    """Convert a persona JSON dict into a system-prompt string (labeled
    sections, example responses at the end)."""
    parts: list = []

    identity = data.get("identity", {})
    voice = data.get("voice", {})
    overview = data.get("overview", {})
    non_neg = data.get("non_negotiable", {})
    bounds = data.get("boundaries", {})
    relations = data.get("relationships", {})
    knowledge = data.get("knowledge_philosophy", {})
    archetype_block = data.get("archetype", {})
    institutional = data.get("institutional_role", {})
    test_cases = data.get("test_cases", [])
    archetype_refs = data.get("archetype_references", [])

    name = identity.get("name", "")
    title = identity.get("title", "")
    institution = identity.get("institution", "UTETY")
    one_line = identity.get("one_line_description", "")
    dept = identity.get("department", "") or institutional.get("department", "")
    location = institutional.get("physical_location", "")

    if title:
        parts.append(f"You are {name}, {title} at {institution}.")
    else:
        parts.append(f"You are {name} of {institution}.")
    if one_line:
        parts.append(one_line)

    arch_human = archetype_block.get("human_archetype", "")
    arch_trait = overview.get("defining_trait", "")
    arch_refs_str = ", ".join(archetype_refs) if archetype_refs else ""
    if arch_human or arch_refs_str:
        arch_line = f"ARCHETYPE: {arch_human}"
        if arch_refs_str:
            arch_line += f" ({arch_refs_str})"
        if arch_trait:
            arch_line += f" — {arch_trait}"
        parts.append(arch_line)

    if dept:
        dept_line = f"DEPARTMENT: {dept}"
        if location:
            dept_line += f". {location}."
        parts.append(dept_line)

    purpose = overview.get("purpose", "")
    if purpose:
        parts.append(purpose)

    principle = non_neg.get("principle_one_sentence", "")
    why = non_neg.get("why_they_hold_it", [])
    practice = non_neg.get("what_it_looks_like_in_practice", [])
    if principle:
        section = [f"CORE PRINCIPLE: {principle}"]
        if why:
            section.append("Why: " + " ".join(why))
        if practice:
            section.append("In practice:")
            for item in practice:
                section.append(f"- {item}")
        parts.append("\n".join(section))

    core_tone = voice.get("core_tone", "")
    characteristics = voice.get("characteristics", [])
    sig_phrases = voice.get("signature_phrases", [])
    if core_tone or characteristics:
        voice_parts = []
        if core_tone:
            voice_parts.append(core_tone)
        voice_parts.extend(characteristics)
        parts.append("VOICE: " + " ".join(voice_parts))
    if sig_phrases:
        parts.append("SIGNATURE PHRASES: " + " / ".join(f'"{p}"' for p in sig_phrases))

    will_always = bounds.get("will_always_do", [])
    wont_do = bounds.get("wont_do", [])
    if will_always:
        parts.append("WILL ALWAYS:\n" + "\n".join(f"- {x}" for x in will_always))
    if wont_do:
        parts.append("WILL NEVER:\n" + "\n".join(f"- {x}" for x in wont_do))

    stance = knowledge.get("stance_on_uncertainty", "")
    teaching_style = knowledge.get("teaching_style", [])
    credentials = knowledge.get("credentials_philosophy", "")
    if teaching_style:
        parts.append("TEACHING APPROACH:\n" + "\n".join(f"- {x}" for x in teaching_style))
    if stance:
        parts.append(f"ON UNCERTAINTY: {stance}")
    if credentials:
        parts.append(f"ON CREDENTIALS: {credentials}")

    courses = institutional.get("courses_taught", [])
    if courses:
        parts.append("TEACHES:\n" + "\n".join(f"- {c}" for c in courses))

    rel_parts = []
    for key, label in (
        ("curious_beginners", "Curious beginners"),
        ("anxious_learner", "Anxious learner"),
        ("tinkerers_makers", "Tinkerers/makers"),
        ("experts_professionals", "Experts"),
        ("children", "Children"),
    ):
        if relations.get(key):
            rel_parts.append(f"{label}: {relations[key]}")
    if rel_parts:
        parts.append("RELATIONSHIPS:\n" + "\n".join(rel_parts))

    deeper_why = archetype_block.get("deeper_why", "")
    closing = archetype_block.get("closing_image", "")
    if deeper_why:
        parts.append(f"DEEPER WHY: {deeper_why}")
    if closing:
        parts.append(f"IMAGE: {closing}")

    fac_rel = overview.get("relationship_to_other_faculty", "") or institutional.get(
        "relationship_to_other_faculty", ""
    )
    if fac_rel:
        parts.append(f"FACULTY RELATIONSHIPS: {fac_rel}")

    # Product role — how the character sits in front of users (folded in from
    # the hand-authored ask-jeles/utety-chat prose during the #18 combine).
    product_role = institutional.get("product_role", "")
    if product_role:
        parts.append(f"ROLE IN THE PRODUCT: {product_role}")

    if test_cases:
        examples = [tc.get("character_response", "") for tc in test_cases]
        examples = [e for e in examples if e]
        if examples:
            parts.append(
                "EXAMPLE RESPONSES (correct register):\n" + "\n".join(f"- {e}" for e in examples)
            )

    _append_closing_discipline(parts, data.get("closing_discipline"))

    return "\n\n".join(p for p in parts if p.strip())
