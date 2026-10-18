"""SQLite schema shared by import, tools, and the chat application."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


_SOURCES = {
    "areas": ("area_id", "name TEXT, team_id TEXT, center_lat REAL, center_lon REAL"),
    "teams": ("team_id", "name TEXT, lead_user_id TEXT"),
    "users": ("user_id", "name TEXT, role TEXT, team_id TEXT"),
    "wells": ("asset_id", "name TEXT, asset_category TEXT, well_type TEXT, area_id TEXT, team_id TEXT, in_charge_user_id TEXT, latitude REAL, longitude REAL, installed_on TEXT, recorded_status TEXT"),
    "equipment": ("equipment_id", "asset_id TEXT, kind TEXT, model TEXT"),
    "inspections": ("inspection_id", "asset_id TEXT, inspected_at TEXT, inspector_user_id TEXT, method TEXT, ambient_temp_c REAL, wellhead_temp_c REAL, spill_detected INTEGER CHECK(spill_detected IN (0,1)), spill_area_pct REAL, spill_est_volume_bbl REAL, evidence_images_json TEXT, notes TEXT"),
    "maintenance": ("maintenance_id", "asset_id TEXT, started_at TEXT, completed_at TEXT, maintenance_type TEXT, equipment_kind TEXT, issue TEXT, action_taken TEXT, technician_user_id TEXT, contractor TEXT, downtime_hours REAL, cost_labor REAL, cost_parts REAL, cost_total REAL, triggered_by_inspection_id TEXT, report_file TEXT"),
    "regulatory_events": ("event_id", "asset_id TEXT, date TEXT, agency TEXT, event_type TEXT, protocol_id TEXT, related_inspection_id TEXT, detail TEXT, hours_to_report REAL, fine_usd REAL, status TEXT"),
    "context_notes": ("note_id", "asset_id TEXT, created_at TEXT, author_user_id TEXT, note_type TEXT, inspection_id TEXT, text TEXT, valid_from TEXT, valid_to TEXT, origin TEXT NOT NULL DEFAULT 'dataset'"),
}


def initialize(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS datasets (
            dataset_id TEXT PRIMARY KEY, label TEXT NOT NULL, source_path TEXT,
            imported_at TEXT NOT NULL, as_of_date TEXT, counts_json TEXT NOT NULL)""")
        for table, (key, fields) in _SOURCES.items():
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                {key} TEXT NOT NULL, {fields}, raw_json TEXT NOT NULL,
                source_path TEXT NOT NULL, source_line INTEGER,
                PRIMARY KEY(dataset_id, {key}))""")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                document_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
                asset_id TEXT, well_type TEXT, title TEXT, body TEXT NOT NULL,
                PRIMARY KEY(dataset_id, document_id), UNIQUE(dataset_id, path));
            CREATE TABLE IF NOT EXISTS well_types (
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                well_type TEXT NOT NULL, production_delta_c REAL NOT NULL,
                manual_document_id TEXT NOT NULL, rule_quote TEXT, rule_line INTEGER,
                PRIMARY KEY(dataset_id, well_type));
            CREATE TABLE IF NOT EXISTS chat_messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id), user_id TEXT NOT NULL,
                session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                payload_json TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS corrections (
                correction_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id), inspection_id TEXT NOT NULL,
                field TEXT NOT NULL, value_json TEXT NOT NULL, author_user_id TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
                source_note_id TEXT, feedback_kind TEXT);
            CREATE INDEX IF NOT EXISTS idx_inspection_well_time ON inspections(dataset_id, asset_id, inspected_at);
            CREATE INDEX IF NOT EXISTS idx_maintenance_well_time ON maintenance(dataset_id, asset_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_context_well_time ON context_notes(dataset_id, asset_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_context_inspection ON context_notes(dataset_id, inspection_id);
            CREATE INDEX IF NOT EXISTS idx_regulatory_well ON regulatory_events(dataset_id, asset_id);
            CREATE INDEX IF NOT EXISTS idx_chat_scope ON chat_messages(dataset_id, user_id, session_id, message_id);
            CREATE INDEX IF NOT EXISTS idx_chat_user_history ON chat_messages(dataset_id, user_id, created_at, message_id);
            CREATE INDEX IF NOT EXISTS idx_correction_inspection ON corrections(dataset_id, inspection_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_documents_well ON documents(dataset_id, asset_id, kind);
            CREATE TABLE IF NOT EXISTS equipment_replacement_reports (
                report_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
                asset_id TEXT NOT NULL, reported_by_user_id TEXT NOT NULL,
                claimed_source_user_id TEXT, evidence_basis TEXT NOT NULL,
                status TEXT NOT NULL, effective_on TEXT NOT NULL,
                statement TEXT NOT NULL, created_at TEXT NOT NULL, source_note_id TEXT);
            CREATE INDEX IF NOT EXISTS idx_equipment_report_well ON equipment_replacement_reports(dataset_id,asset_id,effective_on);
            CREATE TABLE IF NOT EXISTS user_preferences (
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
                user_id TEXT NOT NULL, preferences_json TEXT NOT NULL,
                updated_at TEXT NOT NULL, source_message_id INTEGER,
                PRIMARY KEY(dataset_id,user_id));
        """)
        # Additive migration: a tag classifies a typed correction, never accepts it.
        if "feedback_kind" not in {row[1] for row in conn.execute("PRAGMA table_info(corrections)")}:
            conn.execute("ALTER TABLE corrections ADD COLUMN feedback_kind TEXT")
        conn.execute("""UPDATE corrections SET feedback_kind=CASE
            WHEN field IN ('wellhead_temp_c','ambient_temp_c') THEN 'sensor_reading_correction'
            WHEN field='spill_detected' THEN 'ai_detection_feedback'
            WHEN field IN ('spill_area_pct','spill_est_volume_bbl') THEN 'ai_estimate_feedback'
            ELSE NULL END WHERE feedback_kind IS NULL""")
