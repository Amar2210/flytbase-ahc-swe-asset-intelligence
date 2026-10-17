"""Reject unsupported production-volume rankings without a model request.

Temperature and spill volume cannot establish barrels of produced oil. Inspect
the selected dataset before making a capability claim, including optional JSON
fields. An unfamiliar production measurement falls through to normal retrieval.
"""
from __future__ import annotations

from contextlib import closing
import re

from copilot.db import connect


_TABLES = ("wells", "inspections", "equipment", "maintenance")
_MEASUREMENT = re.compile(
    r"(?:production|produced|oil|flow|metered).*(?:volume|barrel|bbl|rate|bpd|m3|total)"
    r"|(?:^|[._\[])bpd(?:$|[._\]])", re.I,
)
_OTHER_MEASURE = re.compile(r"spill|leak|cost|price|capacity|threshold|forecast|estimate", re.I)


def unsupported_production_volume(db_path, dataset_id: str, message: str) -> dict | None:
    text = message.lower()
    # Never replace a requested status, spill metric, or explicit proxy.
    if not re.search(r"\boil\b", text) or re.search(r"\b(?:spill|leak|downtime|days?|inspections?|temperature|proxy)\b", text):
        return None
    ranking = bool(re.search(r"\b(?:most|highest|largest|rank|ranking|top)\b", text))
    volume = bool(re.search(r"\b(?:volume|barrels?|bbl|bpd|how much|how many)\b", text))
    production = bool(re.search(r"\b(?:produc\w*|output|yield)\b", text))
    if not production or not (ranking or volume):
        return None

    paths = set()
    inspected = []
    with closing(connect(db_path)) as conn:
        for table in _TABLES:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if not columns or "dataset_id" not in columns:
                continue
            if not conn.execute(f'SELECT 1 FROM "{table}" WHERE dataset_id=? LIMIT 1', (dataset_id,)).fetchone():
                continue
            inspected.append(table)
            paths.update(columns)
            if "raw_json" in columns:
                # Optional source fields remain in raw_json. Inspect their paths
                # too, rather than assuming the relational schema is exhaustive.
                paths.update(row[0] for row in conn.execute(
                    f'SELECT DISTINCT j.fullkey FROM "{table}" AS r, '
                    "json_tree(CASE WHEN json_valid(r.raw_json) THEN r.raw_json ELSE '{}' END) AS j "
                    "WHERE r.dataset_id=? AND j.type NOT IN ('object','array','null')",
                    (dataset_id,),
                ))
        if "inspections" not in inspected:
            return None
        if any(_MEASUREMENT.search(path) and not _OTHER_MEASURE.search(path) for path in paths):
            return None
        # If an imported report might contain a metered production measurement,
        # let the model retrieve and assess that report instead of rejecting it.
        document_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone()
        if document_table and conn.execute(
            "SELECT 1 FROM documents WHERE dataset_id=? AND (lower(body) LIKE '%production%' OR lower(body) LIKE '%produced oil%') "
            "AND (lower(body) LIKE '%bbl%' OR lower(body) LIKE '%barrels%' OR lower(body) LIKE '%bpd%') "
            "AND (lower(body) LIKE '%meter%' OR lower(body) LIKE '%measured production%') LIMIT 1", (dataset_id,),
        ).fetchone():
            return None

    source = {
        "id": "S1", "label": "S1 · Imported production measurement coverage",
        "type": "dataset_schema", "dataset_id": dataset_id,
        "table": "inspections", "record_id": "production-measurement-coverage",
        "content": "Checked selected-dataset relational columns and optional raw JSON fields in: "
                   + ", ".join(inspected) + ". No measured oil-production volume or flow-rate field was found. "
                   "Temperature readings and spill estimates are separate measurements.",
    }
    return {
        "answer": "I cannot determine which well produced the most oil from these records. "
                  "The imported structured records have no measured oil-production volume or flow-rate field. [S1]\n\n"
                  "Temperature readings indicate whether a well was producing at an inspection. "
                  "They do not measure barrels of oil. Estimated spill volume is also a different measurement.\n\n"
                  "I can rank wells by producing inspection count or inspection-based producing days if you choose one of those proxies. "
                  "A ranking by oil volume requires measured production volumes, or flow rates with time coverage.",
        "citations": [source], "visuals": [], "pending_update": None,
        "tool_calls": [{"tool": "check_production_measurement_coverage", "arguments": {},
                        "status": "completed", "mode": "local dataset capability check"}],
        "capability_limit": "measured_oil_production_volume_unavailable",
    }
