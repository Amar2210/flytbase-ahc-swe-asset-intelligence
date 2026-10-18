"""Conservative local extraction of explicit correction *proposals*.

This is deliberately smaller than a natural-language parser. Anything unclear
falls through to the model, which can ask for clarification. A returned value
still needs the application's inspection checks and human confirmation; this
module never reads or changes a record.
"""
from __future__ import annotations

import math
import re


_INSPECTION_ID = r"INS-\d{1,8}-\d{1,8}"
_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_BOOL = r"(?:true|false|yes|no)"
_UNIT = r"(?:°\s*[a-z]+|degrees?\s+[a-z]+|[a-z]+|%)"
_VALUE = rf"(?P<value>{_NUMBER}|{_BOOL})(?:\s*(?P<unit>{_UNIT}))?"
_FIELD_ALIASES = {
    "wellhead_temp_c": r"(?:wellhead_temp_c|wellhead\s+(?:temperature|temp(?:\.)?))",
    "ambient_temp_c": r"(?:ambient_temp_c|ambient\s+(?:temperature|temp(?:\.)?))",
    "spill_detected": r"(?:spill_detected|spill\s+(?:detected|flag|detection(?:\s+flag)?))",
    "spill_area_pct": r"(?:spill_area_pct|spill\s+area(?:\s+(?:percentage|percent))?)",
    "spill_est_volume_bbl": r"(?:spill_est_volume_bbl|spill\s+(?:estimated\s+)?volume)",
}
# Reject partial values such as "41 or 42", ranges, exponents and unsupported units.
_VALUE_END = r"(?=$|[.!;,](?!\d)|\n|\s+(?:because|due\s+to|for|and|reason\b|preserving|based\s+on)\b)"
_COMMAND = r"(?:correct|set|change|save|record|update|apply)"
_NEGATED_WRITE = re.compile(
    rf"\b(?:do\s+not|don['’]t|never|not|avoid|without|no\s+need\s+to)"
    rf"(?:\s+\w+){{0,2}}\s+{_COMMAND}\b|\bnot\s+(?:a\s+)?correction\b|"
    r"\b(?:do\s+not|don['’]t|never)\s+(?:make|persist|write|submit|commit|perform)\b|"
    r"\b(?:no|without)\s+(?:writes?|changes?|updates?)\b|"
    r"\b(?:cancel|discard|withdraw)\b", re.I)
_UNASSERTED = re.compile(
    r"\b(?:if|unless|suppose|assuming|imagine|hypothetical|hypothetically|example|"
    r"maybe|perhaps|possibly|might|could|would|should|probably)\b", re.I)


def _without_quotes(text: str) -> str:
    """Keep spans stable while ignoring commands quoted as evidence."""
    pattern = r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)\'[^\'\n]*\'(?!\w)|`[^`\n]*`'
    return re.sub(pattern, lambda match: " " * len(match[0]), text)


def _reason(text: str) -> str | None:
    """Keep the operator's stated cause, without inventing one."""
    match = re.search(r"\b(?:because|due\s+to|reason\s*:)\s*(.+)", text, re.I | re.S)
    if not match:
        return None
    # Whitespace after punctuation keeps decimal readings intact.
    reason = re.split(r"(?<=[.!?;])\s+|\n", match[1], maxsplit=1)[0].strip()
    return reason if len(reason.strip(" .;:")) >= 3 else None


def _typed_value(field: str, value: str, unit: str | None):
    normalized_unit = re.sub(r"\s+", "", unit or "").lower()
    if field == "spill_detected":
        if normalized_unit or value.lower() not in {"true", "false", "yes", "no"}:
            return None
        return value.lower() in {"true", "yes"}
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    if field.endswith("temp_c"):
        allowed = {"", "c", "°c", "celsius", "degreec", "degreesc", "degreecelsius", "degreescelsius"}
        valid = -100 <= number <= 500
    elif field == "spill_area_pct":
        allowed, valid = {"", "%", "percent", "percentage"}, 0 <= number <= 100
    else:
        allowed, valid = {"", "bbl", "barrel", "barrels"}, 0 <= number <= 1_000_000
    return number if normalized_unit in allowed and valid else None


def parse_explicit_correction(text: str) -> dict | None:
    """Extract a single fully specified ``correct_inspection`` proposal.

    Supported requests name one exact inspection, a replacement numeric/boolean
    field, and an explicit reason. A question, hypothetical, quoted instruction,
    unsupported unit or competing target is intentionally left to the model.
    """
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
        return None
    request = text.strip()
    if "?" in request or "```" in request:
        return None
    plain = _without_quotes(request)
    if not re.match(rf"^(?:please\s+)?{_COMMAND}\b", plain, re.I):
        return None
    if _UNASSERTED.search(plain) or _NEGATED_WRITE.search(plain):
        return None
    # Do not quietly ignore an alternative inspection mentioned in a quote.
    inspection_ids = {match[0].upper() for match in re.finditer(rf"\b{_INSPECTION_ID}\b", request, re.I)}
    if len(inspection_ids) != 1:
        return None
    reason = _reason(plain)
    if not reason:
        return None

    candidates = set()
    for field, alias in _FIELD_ALIASES.items():
        target_prefix = (
            rf"\b(?:set|correct|change|update)\s+(?:the\s+)?{alias}\s*"
            rf"(?:(?:for|in|on)\s+(?:inspection\s+)?{_INSPECTION_ID}\s*)?"
        )
        coordinated_prefix = rf"(?:\band\b|,)\s+(?:the\s+)?{alias}\s*"
        # A valid assignment must not hide a second, malformed correction.
        assignment_spans = [match.span() for match in re.finditer(
            rf"(?:{target_prefix}|{coordinated_prefix})(?:to|=|from)\s*|"
            rf"\b(?:save|record|use|apply)\s+[^\n!?;]{{1,160}}?\s+as\s+"
            rf"(?:the\s+)?(?:corrected|new|replacement)\s+{alias}\b", plain, re.I)]
        matched_spans = []
        patterns = [
            # Save 41 C as the corrected wellhead temperature.
            rf"\b(?:save|record|use|apply)\s+{_VALUE}\s+as\s+(?:the\s+)?"
            rf"(?:corrected|new|replacement)\s+{alias}\b",
            # Set wellhead temperature [for INS-...] to 41 C.
            rf"{target_prefix}(?:to|=)\s*{_VALUE}{_VALUE_END}",
            # Correct wellhead temperature from 24 C to 41 C.
            rf"{target_prefix}from\s+(?P<old_value>{_NUMBER})"
            rf"(?:\s*(?P<old_unit>{_UNIT}))?\s+to\s+{_VALUE}{_VALUE_END}",
            rf"{coordinated_prefix}(?:to|=)\s*{_VALUE}{_VALUE_END}",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, plain, re.I):
                value = _typed_value(field, match["value"], match["unit"])
                if value is None:
                    # Do not silently drop unsupported assignments.
                    return None
                if "old_value" in match.groupdict() and _typed_value(field, match["old_value"], match["old_unit"]) is None:
                    return None
                candidates.add((field, value))
                matched_spans.append(match.span())
        if any(not any(start <= mentioned_start and mentioned_end <= end
                       for start, end in matched_spans)
               for mentioned_start, mentioned_end in assignment_spans):
            return None
    if len(candidates) != 1:
        return None
    field, value = candidates.pop()
    return {"inspection_id": inspection_ids.pop(), "field": field, "value": value, "reason": reason}
