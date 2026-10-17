"""Select a small tool catalogue without changing tool authorization or data scope."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ContextProfile:
    names: frozenset[str]
    sql_schema: bool


def context_profile(prompt: str, history: list[dict], *, write_requested: bool) -> ContextProfile:
    """Prefer compound domain reads; retain general reads for mixed questions.

    This is catalogue selection, not an answer classifier. Ambiguous requests keep
    SQL and document tools. Recent wording is used only for short follow-ups.
    Existing server-side write checks remain the authorization boundary.
    """
    text = prompt.lower()
    preference_instruction = write_requested and bool(re.search(r"\b(?:preferences?|just for me|remember my)\b", text))
    if not preference_instruction and len(text) < 160 and re.search(r"\b(?:those|these|them|that|same|again|it|also)\b", text):
        previous = [m.get("content", "") for m in history[:-1] if m.get("role") == "user"]
        if previous:
            text += " " + previous[-1].lower()[-1500:]
    recall = bool(re.search(r"\brecall(?:ed|s)?\b|\bpump\b.{0,60}\b(?:replac|swapp)", text))
    private = bool(re.search(r"\b(?:we (?:discussed|talked)|i (?:asked|said|told)|our (?:chat|conversation|discussion)|my (?:chat|conversation|history)|discussed|previous conversation|earlier|last time)\b", text))
    private |= bool(re.search(r"\b(?:yesterday|last week|this week)\b", text) and re.search(r"\b(?:we|i|discuss|chat|conversation|ask|asked|talk)\b", text))
    preference = bool(re.search(r"\b(?:preferences?|just for me|remember my)\b", text))
    compliance = bool(re.search(r"\b(?:spill|containment|contained)\b", text) and re.search(r"\b(?:sop|limit|deadline|compliance|contained|containment)\b", text))
    # These features need records beyond the current-recall compound tool.
    extra_records = bool(re.search(r"\b(?:maintenance|repairs?|overdue|follow[- ]?ups?|scheduled?|contractors?|technicians?|work[- ]order history)\b", text))
    historical = (not write_requested and bool(re.search(
        r"\b(?:historical|history|previously|yesterday|last (?:week|month|year)|"
        r"(?:in|during|before|after|between|as of|on)\s+(?:\d{4}\b|last\b)|used to)\b", text)))
    mixed = extra_records or historical or bool(re.search(r"\b(?:spills?|temperature|producing|production|downtime|fines?|inspection|regulatory|compare|trend|history of)\b", text))
    only_preference = preference and write_requested and not re.search(r"\b(?:recall|inspection|spill|replace|maintenance)\b", text)
    only_compliance = (compliance and bool(re.search(r"\b(?:contained|containment)\b", text))
                       and bool(re.search(r"\b(?:block|area|zone)\b", text))
                       and not extra_records and not historical
                       and not re.search(r"\b(?:temperature|producing|production|downtime|fines?|costs?|pump|recall|compare|trend)\b", text))
    general = not (recall and not mixed) and not only_preference and not only_compliance
    names = {"query_records", "get_well", "search_documents", "get_document"} if general else set()
    if recall:
        names.add("get_recalled_pumps")
    if compliance:
        names.add("get_spill_compliance")
    if private or any("not complete history" in m.get("content", "") for m in history):
        names.add("search_my_conversations")
    if preference:
        names.add("get_my_preferences")
    if write_requested:
        if only_preference:
            names.add("save_my_preferences")
        else:
            names.update({"add_shared_note", "correct_inspection"})
            if recall or re.search(r"\b(?:pump|equipment|swapped|replacement)\b", text):
                names.add("record_equipment_replacement")
            if preference:
                names.add("save_my_preferences")
    return ContextProfile(frozenset(names), "query_records" in names)
