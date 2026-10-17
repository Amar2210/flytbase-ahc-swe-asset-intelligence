"""Grounded domain tools. The model never receives an unrestricted DB connection.

The SQL tool queries an in-memory copy of this dataset's public records. Private
chat history and other datasets are never copied into that database. This also
avoids relying on fragile SQL text rewriting for tenant isolation.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
import base64
import hashlib
import json
import math
import re
import sqlite3
import time
from uuid import uuid4

from copilot.history import resolve_history_window, timestamp_us


PUBLIC_TABLES = (
    "areas", "teams", "users", "well_types", "wells", "equipment", "inspections",
    "maintenance", "regulatory_events", "context_notes", "corrections", "documents",
)
ID_FIELDS = {
    "areas": "area_id", "teams": "team_id", "users": "user_id",
    "well_types": "well_type", "wells": "asset_id", "equipment": "equipment_id",
    "inspections": "inspection_id", "maintenance": "maintenance_id",
    "regulatory_events": "event_id", "context_notes": "note_id",
    "corrections": "correction_id", "documents": "document_id",
}
CORRECTABLE_FIELDS = {
    "wellhead_temp_c", "ambient_temp_c", "spill_detected", "spill_area_pct",
    "spill_est_volume_bbl",
}
FEEDBACK_KINDS = {
    "wellhead_temp_c": "sensor_reading_correction", "ambient_temp_c": "sensor_reading_correction",
    "spill_detected": "ai_detection_feedback", "spill_area_pct": "ai_estimate_feedback",
    "spill_est_volume_bbl": "ai_estimate_feedback",
}
SAFE_FUNCTIONS = set("""abs avg char coalesce concat concat_ws count date datetime
    dense_rank first_value format glob group_concat hex ifnull iif instr julianday
    json json_array json_array_length json_extract json_group_array json_group_object
    json_insert json_object json_patch json_quote json_remove json_replace json_set
    json_type json_valid lag last_value lead length like likelihood likely lower
    ltrim max min nth_value ntile nullif percent_rank printf rank replace round
    row_number rtrim sign strftime substr substring sum time timediff total trim
    typeof unicode unixepoch unlikely upper""".split())

BRIEF_TOOL_DESCRIPTIONS = {
    "get_well": "One well: latest inspection, effective production, corrections, fresh shared notes, ownership and evidence.",
    "get_recalled_pumps": "Complete current recalled-pump table: supplier notice + latest completed installation + shared reports. Hearsay remains listed. my_area=current user's team; include_install_cost=installation-job total with private formatting. Use this instead of separate recall/installation searches.",
    "record_equipment_replacement": "WRITE proposal, UI confirmation required. Attribute explicit completed replacement to selected user; preserve maintenance. 'X told me'=hearsay with X as claimed_source_name; keep recalled until confirmed. Dates use operational TODAY.",
    "save_my_preferences": "WRITE PRIVATE: save only this user's explicitly requested sorting/cost units across conversations.",
    "get_my_preferences": "Read selected user's private sorting and cost units.",
    "get_spill_compliance": "One complete containment check: SR-02 SOP + effective spills + actual report containment times. Returns breach/compliant/unknown evidence; repair duration is not containment. Inclusive dates, default last 90 calendar days through evaluation date.",
    "query_records": "Read-only SQLite SELECT, complete selected dataset; effective_inspections for production/spills. Aggregate before LIMIT; output max 300 rows. Private chats/other datasets inaccessible.",
    "search_documents": "Lexical search of SOP/manual/report passages with source lines. Filter asset_id/well_type; fetch exact document only for missing details.",
    "get_document": "Read exact imported dataset-relative path with source lines; no filesystem access.",
    "add_shared_note": "WRITE proposal for explicit operational report, UI confirmation required. Attributed claim, no sensor override or proven restart. Questions/hypotheticals cannot write.",
    "correct_inspection": "WRITE proposal for exact inspection, field, value and verified reason; UI confirmation required. Preserve original; conflicting corrections disputed.",
    "search_my_conversations": "Selected user's private history across all chats. Dates use runtime field-local TODAY; Monday weeks. Inclusive date bounds, newest first. Omit query for full dated recall. Follow next_cursor with cursor-only calls until has_more=false; never claim omitted history reviewed.",
    "recent_my_conversations": "Latest private messages for selected user; use search_my_conversations for dates or complete pagination.",
}


def normalize_asset_id(value: str) -> str:
    """Accept the WELL-001 shorthand without silently matching arbitrary text."""
    match = re.fullmatch(r"\s*WELL[-_ ]?(\d{1,8})\s*", str(value), re.I)
    if not match:
        raise ValueError("Use a well ID such as WELL-0001.")
    return f"WELL-{int(match.group(1)):04d}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _false_spill(text: str) -> int:
    return int(bool(re.search(
        r"\b(?:not\s+(?:a\s+)?(?:crude\s+|oil\s+)?spill|no\s+crude|"
        r"false[ -]positive|not\s+(?:crude|oil)\b)", text or "", re.I)))


def _containment_evidence(body: str) -> list[dict]:
    """Read explicit elapsed containment times, never repair/reporting durations.

    This deliberately accepts a narrow statement of elapsed time from discovery
    or inspection. A deadline, an unsupported paraphrase, or a missing value is
    unknown rather than a guessed containment time.
    """
    pattern = re.compile(
        r"\bcontained\s+(?:(?:about|approximately|roughly)\s+)?"
        r"(?P<value>\d+(?:\.\d+)?)\s*"
        r"(?P<unit>hours?|hrs?|h|minutes?|mins?)\s+"
        r"(?:after|following|from)\s+(?:the\s+)?"
        r"(?:inspection(?:\s+flight)?|discovery|detection)\b", re.I)
    evidence = []
    for number, line in enumerate(body.splitlines(), 1):
        plain = re.sub(r"[*_`]", "", line)
        for match in pattern.finditer(plain):
            prefix = plain[max(0, match.start() - 50):match.start()]
            if re.search(r"\b(?:not|never|must|should|will|would|could|may|might)(?:\s+\w+){0,2}\s*$", prefix, re.I):
                continue
            value = float(match["value"])
            if match["unit"].lower().startswith("min"):
                value /= 60
            if not math.isfinite(value):
                continue
            evidence.append({"hours": value, "line": number, "quote": match[0]})
    return evidence


class ToolService:
    MAX_ROWS = 300

    def __init__(self, db_path, dataset_id, user_id, as_of_date="2026-10-16", *, field_timezone="UTC"):
        self.db_path = str(db_path)
        self.dataset_id = dataset_id
        self.user_id = user_id
        self.as_of_date = date.fromisoformat(str(as_of_date)).isoformat()
        self.field_timezone = field_timezone
        # Copilot fixes this clock for one request; direct tool use defaults to now.
        self.history_reference_now = None

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _check_session(self, conn):
        if not conn.execute("SELECT 1 FROM datasets WHERE dataset_id=?", (self.dataset_id,)).fetchone():
            raise ValueError("Unknown dataset.")
        if not conn.execute("SELECT 1 FROM users WHERE dataset_id=? AND user_id=?",
                            (self.dataset_id, self.user_id)).fetchone():
            raise ValueError("Choose a user belonging to the selected dataset.")

    @staticmethod
    def _columns(conn, table):
        return [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]

    def tool_definitions(self, names=None):
        def tool(name, description, properties, required=()):
            return {"name": name, "description": description, "parameters": {
                "type": "object", "properties": properties, "required": list(required),
                "additionalProperties": False}}
        text = {"type": "string"}
        definitions = [
            tool("get_well", "Get current inspection-based status, ownership, corrections, shared notes, open repairs and evidence for one well.", {"asset_id": text}, ("asset_id",)),
            tool("get_recalled_pumps", "Find recalled pumps still installed as of evaluation date, using supplier notice, latest completed Parts installed reports and shared replacement claims. Always use this for current recall lists; hearsay is flagged and retained. my_area uses this user's current team. Returns complete table rows and citations; include_install_cost adds installation-job total in the user's saved currency format.", {"my_area": {"type": "boolean"}, "include_install_cost": {"type": "boolean"}}),
            tool("record_equipment_replacement", "WRITE: Propose an explicitly reported completed pump replacement. A direct report becomes an attributed shared replacement after UI confirmation. 'X told me' is hearsay: identify X as claimed_source_name, save unconfirmed and retain on recall list. Never invent a work order or alter original maintenance. Relative dates use evaluation date.", {"asset_id": text, "effective_on": {"type": "string", "description": "YYYY-MM-DD; for yesterday subtract one day from evaluation date."}, "statement": text, "evidence_basis": {"type": "string", "enum": ["direct_report", "hearsay"]}, "claimed_source_name": {"type": ["string", "null"]}}, ("asset_id", "effective_on", "statement", "evidence_basis")),
            tool("save_my_preferences", "WRITE PRIVATE: Remember this user's explicit well-list sorting and cost-display preferences across conversations. Never share these with other users. Only supply preferences actually requested.", {"well_sort": {"type": "string", "enum": ["area_then_well_id", "well_id"]}, "cost_display": {"type": "string", "enum": ["usd", "thousands_usd"]}}),
            tool("get_my_preferences", "Read only the selected user's saved private well-list sorting and cost formatting preferences.", {}),
            tool("get_spill_compliance", "Check spill containment against the imported SR-02 SOP for an area, in one read. Joins effective spills to repair reports, extracts actual elapsed containment hours, and returns breaches, compliant and unknown counts with source lines. Never substitutes repair duration or reporting delay. Dates are inclusive; default is 90 calendar days ending on evaluation date.", {
                "area": {"type": "string", "description": "Exact geographic area name or area_id."},
                "start_date": {"type": "string", "description": "Inclusive YYYY-MM-DD; omit to derive from days."},
                "end_date": {"type": "string", "description": "Inclusive YYYY-MM-DD; defaults to evaluation date."},
                "days": {"type": "integer", "minimum": 1, "maximum": 3660},
                "limit": {"type": "integer", "minimum": 1, "maximum": 300},
            }, ("area",)),
            tool("query_records", "Run a read-only SQLite SELECT over the complete selected dataset. Use effective_inspections for production and spills. Aggregations cover all matching rows; output is capped at 300 rows. Other datasets and all private chat messages are inaccessible.", {"sql": text}, ("sql",)),
            tool("search_documents", "Find manual, SOP and repair report passages using lexical search. Include asset_id for a well's reports. Return paths and line citations; read the full document when needed.", {"query": text, "asset_id": text, "well_type": text, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ("query",)),
            tool("get_document", "Read an imported document by its exact dataset-relative path, with line numbers. No filesystem access.", {"path": text}, ("path",)),
            tool("add_shared_note", "WRITE: Record an explicit operational update for all users. Do not use for questions, hypotheticals or ordinary chat. Requires UI confirmation. A note is a reported claim and does not override sensor data or prove restart after expiry.", {"asset_id": text, "text": text, "valid_from": {"type": "string", "description": "YYYY-MM-DD"}, "valid_to": {"type": ["string", "null"]}, "note_type": {"type": "string", "enum": ["operator_note", "operator_feedback"]}}, ("asset_id", "text", "valid_from")),
            tool("correct_inspection", "WRITE: Preserve and apply a user-confirmed correction against an exact inspection and field. Original values remain intact. Requires UI confirmation; a competing correction becomes disputed rather than silently winning.", {"inspection_id": text, "field": {"type": "string", "enum": sorted(CORRECTABLE_FIELDS)}, "value": {"type": ["number", "boolean", "null"]}, "reason": text}, ("inspection_id", "field", "value", "reason")),
            tool("search_my_conversations", "Search this selected user's private messages across ALL their conversations in this dataset, including older history. For 'the three wells we discussed yesterday', use period='yesterday' without query unless keywords are needed. Periods use runtime field-local TODAY, never analysis_as_of; weeks run Monday-Sunday. Explicit start_date/end_date are inclusive local calendar dates, converted to exact UTC [start,end) bounds. Optional query requires all literal whitespace-separated keywords; asset_id matches well IDs including shorthand. Results are newest first, with IDs, timestamps and private sources. Follow next_cursor until has_more=false before claiming complete coverage; continuation calls supply only cursor and optional limit. No user/dataset/session selector is permitted.", {
                "period": {"type": "string", "enum": ["today", "yesterday", "this week", "last week"]},
                "start_date": {"type": "string", "description": "Inclusive local date, YYYY-MM-DD; omit period."},
                "end_date": {"type": "string", "description": "Inclusive local date, YYYY-MM-DD; omit period."},
                "query": {"type": "string", "description": "Optional literal keywords, all must occur in each matching message; omit for full date-window recall."},
                "asset_id": {"type": "string", "description": "Optional exact well entity, e.g. WELL-0001; shorthand WELL-1 is normalized."},
                "role": {"type": "string", "enum": ["user", "assistant"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                "cursor": {"type": "string", "description": "Use returned next_cursor, with no filters; retains original filters, clock window and snapshot."},
            }),
            tool("recent_my_conversations", "Compatibility shortcut for the selected user's latest private messages in this dataset (up to 30). Use search_my_conversations for dated, filtered, or complete paginated retrieval.", {"limit": {"type": "integer", "minimum": 1, "maximum": 30}}),
        ]
        for definition in definitions:
            definition["description"] = BRIEF_TOOL_DESCRIPTIONS[definition["name"]]
        return [definition for definition in definitions if names is None or definition["name"] in names]

    def execute(self, name, arguments):
        if name not in {d["name"] for d in self.tool_definitions()}:
            return {"error": "Unknown tool.", "tool": name}
        if not isinstance(arguments, dict):
            return {"error": "Tool arguments must be an object."}
        try:
            with self._connect() as conn:
                self._check_session(conn)
            return getattr(self, name)(**arguments)
        except (ValueError, TypeError, sqlite3.Error, OverflowError) as exc:
            return {"error": str(exc), "tool": name}

    def schema_context(self, *, include_sql=True):
        header = f"Selected dataset: {self.dataset_id}. Evaluation date: {self.as_of_date}."
        if not include_sql:
            return header
        with self._connect() as conn:
            internal = {"dataset_id", "source_path", "source_line", "evidence_images_json", "origin"}
            schema = "\n".join(f"{t}({','.join(c for c in self._columns(conn,t) if c not in internal)})"
                               for t in PUBLIC_TABLES if self._columns(conn, t))
        return f"""{header}
SQLite tables (already dataset-scoped; raw_json retains optional source fields):
{schema}
effective_inspections = inspections + raw_wellhead_temp_c,raw_ambient_temp_c,raw_spill_detected,raw_spill_est_volume_bbl,effective_wellhead_temp_c,effective_ambient_temp_c,temperature_delta_c,production_delta_c,is_producing,effective_spill_detected,effective_spill_est_volume_bbl,effective_spill_area_pct,correction_sources,has_disputed_corrections,well_type.
latest_inspections = latest effective row per asset on/before evaluation date.
effective_* applies accepted runtime corrections/imported false-spill notes; unprefixed/raw fields preserve originals. Join corrections.source_note_id=context_notes.note_id; corrections.status controls acceptance, not the note/feedback_kind tag. Disputed corrections do not apply. operator_feedback is a claim, not a rule.
Use date(inspected_at) BETWEEN inclusive dates; recorded_status can be stale. Manual production_delta_c is a classification threshold, not measured oil volume. Read report_file documents for report-only facts. Source metadata columns remain queryable although omitted here.
"""

    def get_recalled_pumps(self, my_area=False, include_install_cost=False):
        from copilot.recalls import get_recalled_pumps
        return get_recalled_pumps(self, my_area=my_area, include_install_cost=include_install_cost)

    def preview_equipment_replacement(self, arguments):
        from copilot.recalls import preview_equipment_replacement
        return preview_equipment_replacement(self, arguments)

    def record_equipment_replacement(self, asset_id, effective_on, statement, evidence_basis, claimed_source_name=None):
        from copilot.recalls import record_equipment_replacement
        return record_equipment_replacement(self, asset_id=asset_id, effective_on=effective_on,
                                            statement=statement, evidence_basis=evidence_basis,
                                            claimed_source_name=claimed_source_name)

    def save_my_preferences(self, well_sort=None, cost_display=None):
        from copilot.preferences import save_preferences
        return save_preferences(self.db_path, self.dataset_id, self.user_id, well_sort=well_sort,
                                cost_display=cost_display,
                                source_message_id=getattr(self, "current_message_id", None))

    def get_my_preferences(self):
        from copilot.preferences import load_preferences
        preferences = load_preferences(self.db_path, self.dataset_id, self.user_id)
        citations = []
        with self._connect() as conn:
            row = conn.execute("SELECT p.updated_at,p.source_message_id,u.name FROM user_preferences p JOIN users u ON u.dataset_id=p.dataset_id AND u.user_id=p.user_id WHERE p.dataset_id=? AND p.user_id=?", (self.dataset_id, self.user_id)).fetchone()
            if row:
                source = {"type": "private_user_preference", "table": "user_preferences", "record_id": self.user_id,
                          "scope": "private", "user_id": self.user_id, "user_name": row["name"], "updated_at": row["updated_at"]}
                if row["source_message_id"] is not None:
                    source["source_message_id"] = row["source_message_id"]
                citations.append(source)
        return {"scope": "private", "preferences": preferences, "citations": citations}

    def _snapshot(self):
        """Only public current-dataset rows cross this security boundary."""
        memory = sqlite3.connect(":memory:")
        memory.row_factory = sqlite3.Row
        try:
            with self._connect() as source:
                self._check_session(source)
                for table in PUBLIC_TABLES:
                    info = list(source.execute(f"PRAGMA table_info({_quote(table)})"))
                    if not info:
                        continue
                    definitions = ",".join(f"{_quote(r[1])} {r[2] or 'TEXT'}" for r in info)
                    memory.execute(f"CREATE TABLE {_quote(table)} ({definitions})")
                    rows = source.execute(f"SELECT * FROM {_quote(table)} WHERE dataset_id=?", (self.dataset_id,))
                    memory.executemany(f"INSERT INTO {_quote(table)} VALUES ({','.join('?' for _ in info)})", rows)
                    key = ID_FIELDS.get(table)
                    if key:
                        memory.execute(f"CREATE INDEX {_quote('idx_' + table)} ON {_quote(table)}({_quote(key)})")
                    if "asset_id" in [r[1] for r in info] and key != "asset_id":
                        memory.execute(f"CREATE INDEX {_quote('asset_' + table)} ON {_quote(table)}(asset_id)")
            memory.create_function("is_false_spill_note", 1, _false_spill, deterministic=True)
            memory.executescript("""
                CREATE INDEX inspections_time_lookup ON inspections(asset_id,inspected_at);
                CREATE INDEX correction_lookup ON corrections(inspection_id,field,status);
                CREATE INDEX context_lookup ON context_notes(inspection_id,note_type);
                CREATE VIEW _runtime_overlays AS
                  SELECT inspection_id,
                    max(CASE WHEN field='wellhead_temp_c' THEN json_extract(value_json,'$') END) wellhead_temp_c,
                    max(CASE WHEN field='ambient_temp_c' THEN json_extract(value_json,'$') END) ambient_temp_c,
                    max(CASE WHEN field='spill_detected' THEN json_extract(value_json,'$') END) spill_detected,
                    max(CASE WHEN field='spill_area_pct' THEN json_extract(value_json,'$') END) spill_area_pct,
                    max(CASE WHEN field='spill_est_volume_bbl' THEN json_extract(value_json,'$') END) spill_est_volume_bbl,
                    group_concat(correction_id) correction_sources
                  FROM corrections WHERE status='accepted' GROUP BY inspection_id;
                CREATE VIEW _false_spills AS
                  SELECT inspection_id, group_concat(note_id) sources FROM context_notes
                  WHERE note_type='correction' AND inspection_id IS NOT NULL
                  AND origin='dataset'
                  AND is_false_spill_note(text)=1 GROUP BY inspection_id;
                CREATE VIEW effective_inspections AS
                  SELECT i.*, i.wellhead_temp_c raw_wellhead_temp_c,
                    i.ambient_temp_c raw_ambient_temp_c, i.spill_detected raw_spill_detected,
                    i.spill_est_volume_bbl raw_spill_est_volume_bbl,
                    coalesce(r.wellhead_temp_c,i.wellhead_temp_c) effective_wellhead_temp_c,
                    coalesce(r.ambient_temp_c,i.ambient_temp_c) effective_ambient_temp_c,
                    round(coalesce(r.wellhead_temp_c,i.wellhead_temp_c)-coalesce(r.ambient_temp_c,i.ambient_temp_c),6) temperature_delta_c,
                    wt.production_delta_c,
                    CASE WHEN wt.production_delta_c IS NULL THEN NULL ELSE
                      round(coalesce(r.wellhead_temp_c,i.wellhead_temp_c)-coalesce(r.ambient_temp_c,i.ambient_temp_c),6)>=wt.production_delta_c END is_producing,
                    CASE WHEN r.spill_detected IS NOT NULL THEN r.spill_detected
                      WHEN f.inspection_id IS NOT NULL THEN 0 ELSE i.spill_detected END effective_spill_detected,
                    CASE WHEN coalesce(r.spill_detected,CASE WHEN f.inspection_id IS NOT NULL THEN 0 ELSE i.spill_detected END)=0 THEN 0
                      ELSE coalesce(r.spill_est_volume_bbl,i.spill_est_volume_bbl) END effective_spill_est_volume_bbl,
                    coalesce(r.spill_area_pct,i.spill_area_pct) effective_spill_area_pct,
                    trim(coalesce(f.sources,'') || ',' || coalesce(r.correction_sources,''),',') correction_sources,
                    EXISTS(SELECT 1 FROM corrections c WHERE c.inspection_id=i.inspection_id AND c.status='disputed') has_disputed_corrections,
                    w.well_type
                  FROM inspections i JOIN wells w ON w.asset_id=i.asset_id
                  LEFT JOIN well_types wt ON wt.well_type=w.well_type
                  LEFT JOIN _runtime_overlays r ON r.inspection_id=i.inspection_id
                  LEFT JOIN _false_spills f ON f.inspection_id=i.inspection_id;
            """)
            memory.execute("CREATE VIEW latest_inspections AS SELECT e.* FROM effective_inspections e WHERE date(e.inspected_at)<= " +
                           "'" + self.as_of_date + "' AND NOT EXISTS(SELECT 1 FROM inspections newer WHERE newer.asset_id=e.asset_id AND date(newer.inspected_at)<='" +
                           self.as_of_date + "' AND newer.inspected_at>e.inspected_at)")
            memory.commit()
            return memory
        except Exception:
            memory.close()
            raise

    def _citation(self, table, record):
        record = dict(record)
        key = ID_FIELDS.get(table)
        result = {"dataset_id": self.dataset_id, "table": table, "record_id": record.get(key)}
        for col in ("source_path", "source_line", "path", "author_user_id", "created_at"):
            if record.get(col) is not None:
                result[col] = record[col]
        if table == "corrections":
            result.update({k: record.get(k) for k in ("inspection_id", "field", "reason", "status", "feedback_kind")})
        return result

    def query_records(self, sql):
        if not isinstance(sql, str) or len(sql) > 20000 or not re.match(r"\s*(SELECT|WITH)\b", sql, re.I):
            raise ValueError("Use one read-only SELECT or WITH query, at most 20,000 characters.")
        conn = self._snapshot()
        tables_read = set()
        try:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2_000_000)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20000)
            deadline = time.monotonic() + 5
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)

            def authorize(action, arg1, arg2, database, source):
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ:
                    if arg1 not in {*PUBLIC_TABLES, "effective_inspections", "latest_inspections", "_runtime_overlays", "_false_spills"}:
                        return sqlite3.SQLITE_DENY
                    tables_read.add(arg1)
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION:
                    return sqlite3.SQLITE_OK if (arg2 or "").lower() in SAFE_FUNCTIONS | {"is_false_spill_note"} else sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_DENY

            conn.set_authorizer(authorize)
            cursor = conn.execute(sql)
            columns = [c[0] for c in cursor.description]
            collected = cursor.fetchmany(self.MAX_ROWS + 1)
            truncated = len(collected) > self.MAX_ROWS
            rows = [{key: (value[:12000] + "…" if isinstance(value, str) and len(value) > 12000 else value)
                     for key, value in dict(row).items()} for row in collected[:self.MAX_ROWS]]
            conn.set_authorizer(None)
            citations = [{"type": "query", "dataset_id": self.dataset_id, "sql": sql,
                          "tables": sorted(t for t in tables_read if t in PUBLIC_TABLES),
                          "note": "SQL aggregation runs over all matching records before the output cap."}]
            if "well_types" in tables_read:
                # An aggregate may return no well/inspection IDs. Its classification
                # still depends on the manual rule and needs a document citation.
                manuals = conn.execute("SELECT d.*,wt.well_type rule_well_type,wt.rule_quote,wt.rule_line FROM well_types wt JOIN documents d ON d.document_id=wt.manual_document_id ORDER BY wt.well_type LIMIT 10").fetchall()
                for manual in manuals:
                    citations.append({**self._citation("documents", manual),
                                      "well_type": manual["rule_well_type"],
                                      "rule_quote": manual["rule_quote"],
                                      "line_start": manual["rule_line"],
                                      "line_end": manual["rule_line"]})
            seen = set()
            for row in rows:
                for correction_source in str(row.get("correction_sources") or "").split(","):
                    if not correction_source or len(seen) >= 25 or any((table, correction_source) in seen for table in ("context_notes", "corrections")):
                        continue
                    for source_table, source_key in (("context_notes", "note_id"), ("corrections", "correction_id")):
                        source_record = conn.execute(f"SELECT * FROM {source_table} WHERE {source_key}=?", (correction_source,)).fetchone()
                        if source_record:
                            citations.append(self._citation(source_table, source_record))
                            seen.add((source_table, correction_source))
                            break
                for table, key in ID_FIELDS.items():
                    if row.get(key) is None or table not in tables_read:
                        continue
                    pair = (table, str(row[key]))
                    if pair in seen or len(seen) >= 25:
                        continue
                    seen.add(pair)
                    record = conn.execute(f"SELECT * FROM {_quote(table)} WHERE {_quote(key)}=? LIMIT 1", (row[key],)).fetchone()
                    if record:
                        citations.append(self._citation(table, record))
            return {"columns": columns, "rows": rows, "returned_rows": len(rows),
                    "total_rows": None if truncated else len(rows), "truncated": truncated,
                    "citations": citations, "as_of_date": self.as_of_date}
        finally:
            conn.close()

    def get_well(self, asset_id):
        asset_id = normalize_asset_id(asset_id)
        conn = self._snapshot()
        try:
            well = conn.execute("SELECT w.*,a.name area_name,t.name team_name,u.name in_charge_name FROM wells w LEFT JOIN areas a ON a.area_id=w.area_id LEFT JOIN teams t ON t.team_id=w.team_id LEFT JOIN users u ON u.user_id=w.in_charge_user_id WHERE w.asset_id=?", (asset_id,)).fetchone()
            if not well:
                raise ValueError(f"{asset_id} is not in the selected dataset.")
            latest = conn.execute("SELECT e.*,u.name inspector_name FROM latest_inspections e LEFT JOIN users u ON u.user_id=e.inspector_user_id WHERE e.asset_id=? ORDER BY inspected_at DESC LIMIT 1", (asset_id,)).fetchone()
            notes = [dict(r) for r in conn.execute("SELECT n.*,u.name author_name,c.correction_id,c.status correction_status,c.feedback_kind FROM context_notes n LEFT JOIN users u ON u.user_id=n.author_user_id LEFT JOIN corrections c ON c.source_note_id=n.note_id WHERE n.asset_id=? ORDER BY n.created_at DESC LIMIT 40", (asset_id,))]
            for note in notes:
                note["applicability"] = ("future" if note.get("valid_from", "") > self.as_of_date else "expired" if note.get("valid_to") and note["valid_to"] < self.as_of_date else "current")
                if note["note_type"] == "correction" and note.get("correction_status"):
                    note["interpretation"] = "Accepted field correction" if note["correction_status"] == "accepted" else "Disputed correction; not applied"
                else:
                    note["interpretation"] = "Formal dataset correction" if note["note_type"] == "correction" else "Reported context; does not override inspection measurements"
            repairs = [dict(r) for r in conn.execute("SELECT * FROM maintenance WHERE asset_id=? AND date(started_at)<=? AND (completed_at IS NULL OR date(completed_at)>?) ORDER BY started_at DESC LIMIT 12", (asset_id, self.as_of_date, self.as_of_date))]
            corrections = [dict(r) for r in conn.execute("SELECT c.*,u.name author_name FROM corrections c LEFT JOIN users u ON u.user_id=c.author_user_id JOIN inspections i ON i.inspection_id=c.inspection_id WHERE i.asset_id=? ORDER BY c.created_at DESC LIMIT 30", (asset_id,))]
            citations = [self._citation("wells", well)]
            if latest:
                citations.append(self._citation("inspections", latest))
            citations += [self._citation("context_notes", n) for n in notes]
            citations += [self._citation("maintenance", n) for n in repairs]
            citations += [self._citation("corrections", n) for n in corrections]
            manual = conn.execute("SELECT d.* FROM well_types wt JOIN documents d ON d.document_id=wt.manual_document_id WHERE wt.well_type=?", (well["well_type"],)).fetchone()
            if manual:
                citations.append(self._citation("documents", manual))
            warnings = ["Status reflects the latest available inspection, not a live sensor reading.", "An expired maintenance plan does not confirm completion or resumed production."]
            if latest and latest["is_producing"] is None:
                warnings.append("No reliable manual threshold was imported: production status is unknown.")
            if latest and (latest["correction_sources"] or latest["has_disputed_corrections"]):
                warnings.append("Corrections exist. Compare corrected measurements with the original inspector notes and disclose any disagreement.")
            if latest and latest["is_producing"] is not None and bool(latest["is_producing"]) != (well["recorded_status"] == "active"):
                warnings.append("The recorded_status disagrees with the latest inspection-based classification and may be stale.")
            return {"well": dict(well), "latest_inspection": dict(latest) if latest else None,
                    "shared_notes": notes, "open_maintenance": repairs, "corrections": corrections,
                    "warnings": warnings, "as_of_date": self.as_of_date, "citations": citations}
        finally:
            conn.close()

    def get_spill_compliance(self, area, start_date=None, end_date=None, days=90, limit=100):
        """A bounded evidence join for a common multi-source compliance question."""
        area = self._valid_text(area, "Area", 200)
        for value, label, maximum in ((days, "days", 3660), (limit, "limit", self.MAX_ROWS)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{label} must be an integer from 1 to {maximum}.")
        end = date.fromisoformat(end_date) if end_date else date.fromisoformat(self.as_of_date)
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days - 1)
        if end < start:
            raise ValueError("end_date cannot precede start_date.")
        conn = self._snapshot()
        try:
            areas = conn.execute("SELECT area_id,name FROM areas WHERE lower(area_id)=lower(?) OR lower(name)=lower(?)", (area, area)).fetchall()
            if len(areas) != 1:
                raise ValueError("Area must match one exact area name or area_id in the selected dataset.")
            selected = dict(areas[0])
            # The rule comes from an imported SOP protocol row, not this code or
            # a report's historical compliance assessment.
            rule_evidence = []
            rule_pattern = re.compile(r"contained\s+within\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?)\b", re.I)
            for doc in conn.execute("SELECT * FROM documents WHERE kind='sop' ORDER BY path"):
                for number, line in enumerate(doc["body"].splitlines(), 1):
                    plain = re.sub(r"[*_`]", "", line)
                    if not re.match(r"\s*\|\s*SR-02\s*\|", plain, re.I):
                        continue
                    match = rule_pattern.search(plain)
                    if not match:
                        continue
                    value = float(match[1]) / (60 if match[2].lower().startswith("min") else 1)
                    if not math.isfinite(value) or value <= 0:
                        continue
                    rule_evidence.append({"hours": value, "citation": {
                        **self._citation("documents", doc), "line_start": number,
                        "line_end": number, "quote": line.strip()}})
            rule_values = {item["hours"] for item in rule_evidence}
            deadline = next(iter(rule_values)) if len(rule_values) == 1 else None
            rule_issue = (None if deadline is not None else "Conflicting SR-02 containment limits in imported SOPs."
                          if rule_values else "No explicit SR-02 containment deadline found in imported SOP protocol rows.")
            sql = """SELECT i.inspection_id,i.asset_id,i.inspected_at,
                i.effective_spill_est_volume_bbl spill_volume_bbl,
                i.correction_sources,i.has_disputed_corrections,
                i.source_path,i.source_line
                FROM effective_inspections i JOIN wells w ON w.asset_id=i.asset_id
                WHERE w.area_id=? AND i.effective_spill_detected=1
                AND date(i.inspected_at) BETWEEN ? AND ?
                ORDER BY i.inspected_at DESC,i.inspection_id"""
            inspections = conn.execute(sql, (selected["area_id"], start.isoformat(), end.isoformat())).fetchall()
            excluded = conn.execute("""SELECT count(*) FROM effective_inspections i
                JOIN wells w ON w.asset_id=i.asset_id WHERE w.area_id=?
                AND i.spill_detected=1 AND i.effective_spill_detected=0
                AND date(i.inspected_at) BETWEEN ? AND ?""",
                (selected["area_id"], start.isoformat(), end.isoformat())).fetchone()[0]
            rows = []
            row_citations = {}
            for inspection in inspections:
                row = {key: inspection[key] for key in (
                    "inspection_id", "asset_id", "inspected_at", "spill_volume_bbl",
                    "correction_sources", "has_disputed_corrections")}
                reports, elapsed, issues, citations = [], set(), [], []
                repairs = conn.execute("SELECT maintenance_id,report_file FROM maintenance WHERE asset_id=? AND triggered_by_inspection_id=? ORDER BY maintenance_id",
                    (inspection["asset_id"], inspection["inspection_id"])).fetchall()
                for repair in repairs:
                    path = str(repair["report_file"] or "").replace("\\", "/")
                    if not path:
                        issues.append(f"{repair['maintenance_id']} has no linked report.")
                        continue
                    if not path.startswith("assets/"):
                        path = f"assets/{inspection['asset_id']}/{path}"
                    doc = conn.execute("SELECT * FROM documents WHERE path=? AND asset_id=?", (path, inspection["asset_id"])).fetchone()
                    if not doc:
                        issues.append(f"Linked report is unavailable: {path}.")
                        continue
                    # A mismatched explicit inspection reference is unsafe to
                    # transfer to this event even when the maintenance link says otherwise.
                    references = set(re.findall(r"\bINS-[A-Za-z0-9-]+\b", doc["body"]))
                    if references and inspection["inspection_id"] not in references:
                        issues.append(f"{repair['maintenance_id']} references a different inspection.")
                        continue
                    evidence = _containment_evidence(doc["body"])
                    if not evidence:
                        issues.append(f"No explicit elapsed containment time in {repair['maintenance_id']}.")
                    elapsed.update(item["hours"] for item in evidence)
                    reports.append({"maintenance_id": repair["maintenance_id"], "path": path,
                                    "containment_lines": [item["line"] for item in evidence]})
                    citations.extend({**self._citation("documents", doc), "line_start": item["line"],
                                      "line_end": item["line"], "quote": item["quote"]}
                                     for item in evidence)
                if not repairs:
                    issues.append("No maintenance report linked to this inspection.")
                hours = next(iter(elapsed)) if len(elapsed) == 1 else None
                if len(elapsed) > 1:
                    issues.append("Conflicting elapsed containment times in linked reports.")
                # Conflicting/missing evidence never becomes an invented breach.
                status = ("unknown" if deadline is None or hours is None else
                          "breach" if hours > deadline else "within_limit")
                row.update(containment_hours=hours, status=status, reports=reports)
                if hours is not None and deadline is not None:
                    row["hours_over_limit"] = round(max(0, hours - deadline), 6)
                if issues:
                    row["evidence_issues"] = issues
                rows.append(row)
                row_citations[row["inspection_id"]] = [self._citation("inspections", inspection), *citations]
                # Preserve the correction provenance for false-positive overlays
                # and corrected volume estimates without sending entire notes.
                for source_id in str(inspection["correction_sources"] or "").split(","):
                    for table, key in (("context_notes", "note_id"), ("corrections", "correction_id")):
                        source = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (source_id,)).fetchone()
                        if source:
                            row_citations[row["inspection_id"]].append(self._citation(table, source))
                            break
            counts = {status: sum(row["status"] == status for row in rows)
                      for status in ("breach", "within_limit", "unknown")}
            # Breaches and unknown evidence survive the cap before compliant rows.
            rows.sort(key=lambda row: {"breach": 0, "unknown": 1, "within_limit": 2}[row["status"]])
            returned = rows[:limit]
            citations = [item["citation"] for item in rule_evidence]
            for row in returned:
                citations.extend(row_citations[row["inspection_id"]])
            return {"area": selected, "start_date": start.isoformat(), "end_date": end.isoformat(),
                    "as_of_date": self.as_of_date, "date_boundaries": "inclusive calendar dates",
                    "rule": {"protocol_id": "SR-02", "containment_limit_hours": deadline,
                             "issue": rule_issue},
                    "total_spills": len(rows), "counts": counts,
                    "excluded_corrected_detections": excluded,
                    "rows": returned, "returned_rows": len(returned),
                    "truncated": len(rows) > limit, "omitted_rows": max(0, len(rows) - limit),
                    "interpretation": "Counts cover all matching effective spill detections, not deduplicated spill incidents. Actual containment times come only from linked report statements; missing or conflicting times are unknown. Repair duration and reporting delay are never used.",
                    "citations": citations}
        finally:
            conn.close()

    def search_documents(self, query, asset_id=None, well_type=None, limit=5):
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("Provide a search query of 1–1,000 characters.")
        limit = max(1, min(int(limit), 10))
        tokens = list(dict.fromkeys(re.findall(r"[\w-]{2,}", query.lower())))
        if asset_id:
            asset_id = normalize_asset_id(asset_id)
        with self._connect() as conn:
            self._check_session(conn)
            docs = conn.execute("SELECT * FROM documents WHERE dataset_id=? AND (? IS NULL OR asset_id=? OR asset_id IS NULL) AND (? IS NULL OR well_type=? OR well_type IS NULL)", (self.dataset_id, asset_id, asset_id, well_type, well_type)).fetchall()
        ranked = []
        for doc in docs:
            lines = doc["body"].splitlines()
            scores = [sum(line.lower().count(token) for token in tokens) for line in lines]
            score = sum(scores) + 3 * sum(doc["title"].lower().count(token) for token in tokens)
            if not score:
                continue
            best = max(range(len(lines)), key=lambda i: scores[i]) if lines else 0
            start, end = max(0, best - 5), min(len(lines), best + 16)
            excerpt = "\n".join(f"{i+1}: {lines[i]}" for i in range(start, end))
            ranked.append((score, {"document_id": doc["document_id"], "path": doc["path"], "kind": doc["kind"], "title": doc["title"], "asset_id": doc["asset_id"], "well_type": doc["well_type"], "line_start": start + 1, "line_end": end, "excerpt": excerpt[:10000], "citation": {**self._citation("documents", doc), "line_start": start + 1, "line_end": end}}))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"]))
        results = [item[1] for item in ranked[:limit]]
        return {"documents": results, "citations": [r["citation"] for r in results], "matching_documents": len(ranked)}

    def get_document(self, path):
        if not isinstance(path, str) or len(path) > 1000:
            raise ValueError("Provide a dataset document path.")
        with self._connect() as conn:
            self._check_session(conn)
            doc = conn.execute("SELECT * FROM documents WHERE dataset_id=? AND (path=? OR document_id=?)", (self.dataset_id, path.replace("\\", "/"), path)).fetchone()
        if not doc:
            raise ValueError("No imported document has that exact path. Search documents first.")
        lines = doc["body"].splitlines()
        numbered = "\n".join(f"{i+1}: {line}" for i, line in enumerate(lines))
        return {"path": doc["path"], "title": doc["title"], "body": numbered[:40000],
                "truncated": len(numbered) > 40000, "line_count": len(lines),
                "citations": [self._citation("documents", doc)]}

    @staticmethod
    def _valid_text(value, label, maximum=8000):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{label} must contain 1–{maximum} characters.")
        return value.strip()

    def add_shared_note(self, asset_id, text, valid_from, valid_to=None, note_type="operator_note"):
        asset_id = normalize_asset_id(asset_id)
        text = self._valid_text(text, "Note")
        valid_from = date.fromisoformat(valid_from).isoformat()
        valid_to = date.fromisoformat(valid_to).isoformat() if valid_to else None
        if valid_to and valid_to < valid_from:
            raise ValueError("valid_to cannot precede valid_from.")
        if note_type not in ("operator_note", "operator_feedback"):
            raise ValueError("Use correct_inspection for a formal correction.")
        record = {"note_id": "CTX-LIVE-" + uuid4().hex[:16], "dataset_id": self.dataset_id,
                  "asset_id": asset_id, "created_at": _now(), "author_user_id": self.user_id,
                  "note_type": note_type, "inspection_id": None, "text": text,
                  "valid_from": valid_from, "valid_to": valid_to, "origin": "runtime",
                  "source_path": "runtime:shared-context", "source_line": None}
        record["raw_json"] = json.dumps({k: v for k, v in record.items() if k != "dataset_id"})
        with self._connect() as conn:
            self._check_session(conn)
            if not conn.execute("SELECT 1 FROM wells WHERE dataset_id=? AND asset_id=?", (self.dataset_id, asset_id)).fetchone():
                raise ValueError("Well does not belong to the selected dataset.")
            columns = set(self._columns(conn, "context_notes"))
            record = {k: v for k, v in record.items() if k in columns}
            conn.execute(f"INSERT INTO context_notes ({','.join(_quote(k) for k in record)}) VALUES ({','.join('?' for _ in record)})", tuple(record.values()))
        return {"saved": True, "scope": "shared", "note": record,
                "interpretation": "Saved as a reported operational claim. Sensor measurements are unchanged; conflicts must be disclosed. Expiry does not prove restart.",
                "citations": [self._citation("context_notes", record)]}

    def correct_inspection(self, inspection_id, field, value, reason):
        if field not in CORRECTABLE_FIELDS:
            raise ValueError("Unsupported correction field.")
        reason = self._valid_text(reason, "Correction reason")
        if field == "spill_detected":
            if not isinstance(value, bool):
                raise ValueError("spill_detected requires true or false.")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("A finite numerical value is required.")
        elif field.endswith("temp_c") and not -100 <= value <= 500:
            raise ValueError("Temperature must be between -100 and 500 °C.")
        elif field == "spill_area_pct" and not 0 <= value <= 100:
            raise ValueError("Spill area must be between 0 and 100 percent.")
        elif field == "spill_est_volume_bbl" and not 0 <= value <= 1_000_000:
            raise ValueError("Spill volume must be between 0 and 1,000,000 barrels.")
        correction_id = "COR-" + uuid4().hex[:16]
        created_at = _now()
        with self._connect() as conn:
            self._check_session(conn)
            conn.execute("BEGIN IMMEDIATE")
            inspection = conn.execute("SELECT * FROM inspections WHERE dataset_id=? AND inspection_id=?", (self.dataset_id, inspection_id)).fetchone()
            if not inspection:
                raise ValueError("Inspection does not belong to the selected dataset.")
            competing = conn.execute("SELECT * FROM corrections WHERE dataset_id=? AND inspection_id=? AND field=? AND status IN ('accepted','disputed')", (self.dataset_id, inspection_id, field)).fetchall()
            imported_false_notes = []
            if field == "spill_detected" and value is True:
                imported_false_notes = [dict(r) for r in conn.execute("SELECT * FROM context_notes WHERE dataset_id=? AND inspection_id=? AND note_type='correction' AND origin='dataset'", (self.dataset_id, inspection_id)) if _false_spill(r["text"])]
            conflict = any(json.loads(r["value_json"]) != value for r in competing)
            conflict = conflict or bool(imported_false_notes)
            status = "disputed" if conflict else "accepted"
            if conflict:
                conn.execute("UPDATE corrections SET status='disputed' WHERE dataset_id=? AND inspection_id=? AND field=? AND status='accepted'", (self.dataset_id, inspection_id, field))
            source_note_id = "CTX-LIVE-" + uuid4().hex[:16]
            context_note = {"note_id": source_note_id, "dataset_id": self.dataset_id,
                            "asset_id": inspection["asset_id"], "created_at": created_at,
                            "author_user_id": self.user_id, "note_type": "correction",
                            "inspection_id": inspection_id,
                            "text": f"Proposed field correction for {inspection_id}: {field} = {json.dumps(value)}. Original value: {json.dumps(inspection[field])}. Reason: {reason}",
                            "valid_from": str(inspection["inspected_at"])[:10], "valid_to": None,
                            "origin": "runtime", "source_path": "runtime:shared-context", "source_line": None}
            context_note["raw_json"] = json.dumps({k: v for k, v in context_note.items() if k != "dataset_id"})
            conn.execute(f"INSERT INTO context_notes ({','.join(_quote(k) for k in context_note)}) VALUES ({','.join('?' for _ in context_note)})", tuple(context_note.values()))
            record = {"correction_id": correction_id, "dataset_id": self.dataset_id,
                      "inspection_id": inspection_id, "field": field, "value_json": json.dumps(value),
                      "author_user_id": self.user_id, "reason": reason, "created_at": created_at,
                      "status": status, "source_note_id": source_note_id, "feedback_kind": FEEDBACK_KINDS[field]}
            conn.execute(f"INSERT INTO corrections ({','.join(_quote(k) for k in record)}) VALUES ({','.join('?' for _ in record)})", tuple(record.values()))
        return {"saved": True, "correction": record, "original_value": inspection[field],
                "applied": status == "accepted", "competing_correction_ids": [r["correction_id"] for r in competing],
                "conflicting_imported_note_ids": [r["note_id"] for r in imported_false_notes],
                "warning": "Competing values are disputed; original evidence remains in effect until explicitly resolved." if conflict else "Compare the corrected result with the inspector's original notes; disclose disagreement.",
                "original_inspector_notes": inspection["notes"],
                "citations": [self._citation("inspections", inspection), self._citation("corrections", record), self._citation("context_notes", context_note)] + [self._citation("context_notes", r) for r in imported_false_notes]}

    def search_my_conversations(self, period=None, start_date=None, end_date=None,
                                query=None, asset_id=None, role=None, limit=20, cursor=None):
        """A private, scoped keyset search; never make chat rows public SQL tables."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("History page limit must be an integer from 1 to 30.")
        scope = hashlib.sha256(json.dumps([self.dataset_id, self.user_id]).encode()).hexdigest()
        state = None
        if cursor is not None:
            if any(value is not None for value in (period, start_date, end_date, query, asset_id, role)):
                raise ValueError("A history continuation takes only cursor and optional limit; filters are retained.")
            try:
                if not isinstance(cursor, str) or len(cursor) > 16384:
                    raise ValueError
                state = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if state["version"] != 1 or state["scope"] != scope:
                    raise ValueError
                for key in ("snapshot_max_id", "last_timestamp_us", "last_message_id"):
                    if isinstance(state[key], bool) or not isinstance(state[key], int):
                        raise ValueError
                if state["snapshot_max_id"] < 0 or not 0 < state["last_message_id"] <= state["snapshot_max_id"]:
                    raise ValueError
                window, query, asset_id, role = (state[key] for key in ("window", "query", "asset_id", "role"))
                for key in ("start_at_utc", "end_at_utc_exclusive"):
                    if window[key] is not None and timestamp_us(window[key]) is None:
                        raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise ValueError("Invalid history cursor for this selected user and dataset. Start a new search.") from None
        else:
            window = resolve_history_window(period=period, start_date=start_date, end_date=end_date,
                                            timezone_name=self.field_timezone, now=self.history_reference_now)
        if query is not None and (not isinstance(query, str) or len(query) > 500):
            raise ValueError("History query must be at most 500 characters.")
        query = query.strip() if query else ""
        terms = query.casefold().split()
        asset_id = normalize_asset_id(asset_id) if asset_id is not None else None
        if role is not None and role not in ("user", "assistant"):
            raise ValueError("History role must be user or assistant.")

        def matches(content):
            text = content.casefold()
            if not all(term in text for term in terms):
                return False
            if asset_id is not None:
                entities = {normalize_asset_id(match[0]) for match in re.finditer(r"\bWELL[-_ ]?\d{1,8}\b", content, re.I)}
                return asset_id in entities
            return True

        with self._connect() as conn:
            self._check_session(conn)
            snapshot_max_id = state["snapshot_max_id"] if state else conn.execute(
                "SELECT COALESCE(MAX(message_id),0) FROM chat_messages WHERE dataset_id=? AND user_id=?",
                (self.dataset_id, self.user_id)).fetchone()[0]
            conn.create_function("history_timestamp_us", 1, timestamp_us, deterministic=True)
            conn.create_function("history_matches", 1, matches, deterministic=True)
            clauses = ["dataset_id=?", "user_id=?", "message_id<=?", "history_timestamp_us(created_at) IS NOT NULL"]
            parameters = [self.dataset_id, self.user_id, snapshot_max_id]
            for key, operator in (("start_at_utc", ">="), ("end_at_utc_exclusive", "<")):
                if window[key] is not None:
                    clauses.append(f"history_timestamp_us(created_at){operator}?")
                    parameters.append(timestamp_us(window[key]))
            if terms or asset_id:
                clauses.append("history_matches(content)=1")
            if role:
                clauses.append("role=?")
                parameters.append(role)
            if state:
                clauses.append("(history_timestamp_us(created_at)<? OR (history_timestamp_us(created_at)=? AND message_id<?))")
                parameters.extend([state["last_timestamp_us"], state["last_timestamp_us"], state["last_message_id"]])
            rows = conn.execute(
                "SELECT message_id,session_id,role,substr(content,1,16000) content,"
                "length(content)>16000 content_truncated,created_at,history_timestamp_us(created_at) timestamp_us "
                "FROM chat_messages WHERE " + " AND ".join(clauses) +
                " ORDER BY history_timestamp_us(created_at) DESC,message_id DESC LIMIT ?",
                (*parameters, limit + 1)).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more:
            continuation = {"version": 1, "scope": scope, "window": window, "query": query,
                            "asset_id": asset_id, "role": role, "snapshot_max_id": snapshot_max_id,
                            "last_timestamp_us": page[-1]["timestamp_us"], "last_message_id": page[-1]["message_id"]}
            next_cursor = base64.urlsafe_b64encode(json.dumps(continuation, separators=(",", ":")).encode()).decode()
        messages, citations = [], []
        for row in page:
            source = {"dataset_id": self.dataset_id, "table": "chat_messages", "record_id": str(row["message_id"]),
                      "session_id": row["session_id"], "author_user_id": self.user_id,
                      "created_at": row["created_at"], "source_path": "runtime:private-chat", "scope": "private"}
            record = {key: row[key] for key in ("message_id", "session_id", "role", "content", "created_at")}
            record.update(content_truncated=bool(row["content_truncated"]), source=source)
            messages.append(record)
            citations.append(source)
        return {"scope": "private", "dataset_id": self.dataset_id, "user_id": self.user_id,
                "window": window, "filters": {"query": query, "asset_id": asset_id, "role": role},
                "order": "created_at descending, message_id descending", "messages": messages,
                "has_more": has_more, "next_cursor": next_cursor, "citations": citations,
                "note": "Only this selected user's messages in this dataset, across sessions. Continue while has_more before claiming full coverage. Missing history must not be invented; private discussion does not establish current shared well state. Messages with missing/invalid timezone timestamps are excluded."}

    def recent_my_conversations(self, limit=10):
        limit = max(1, min(int(limit), 30))
        with self._connect() as conn:
            self._check_session(conn)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # Chat persistence is deliberately separate from the public SQL snapshot.
            if "chat_messages" in tables:
                rows = conn.execute("SELECT message_id,session_id,role,substr(content,1,16000) content,created_at FROM chat_messages WHERE dataset_id=? AND user_id=? ORDER BY message_id DESC LIMIT ?", (self.dataset_id, self.user_id, limit)).fetchall()
            elif "messages" in tables:
                columns = self._columns(conn, "messages")
                if "user_id" in columns and "dataset_id" in columns:
                    rows = conn.execute("SELECT * FROM messages WHERE dataset_id=? AND user_id=? ORDER BY created_at DESC LIMIT ?", (self.dataset_id, self.user_id, limit)).fetchall()
                elif "conversations" in tables:
                    rows = conn.execute("SELECT m.* FROM messages m JOIN conversations c ON c.conversation_id=m.conversation_id WHERE c.dataset_id=? AND c.user_id=? ORDER BY m.created_at DESC LIMIT ?", (self.dataset_id, self.user_id, limit)).fetchall()
                else:
                    rows = []
            else:
                rows = []
        return {"scope": "private", "user_id": self.user_id, "messages": [dict(r) for r in reversed(rows)],
                "note": "Only this user's own conversations in this dataset are available. If no relevant conversation exists, say so."}
