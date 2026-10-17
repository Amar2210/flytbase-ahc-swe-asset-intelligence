"""Explicit display preferences private to one user in one field dataset."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path

from copilot.db import connect


_OPTIONS = {
    "well_sort": frozenset({"area_then_well_id", "well_id"}),
    "cost_display": frozenset({"usd", "thousands_usd"}),
}


def _known_preferences(value: object) -> dict:
    """Do not load free-form text or unexpected keys into model instructions."""
    if not isinstance(value, dict):
        return {}
    return {key: choice for key, choice in value.items()
            if key in _OPTIONS and isinstance(choice, str) and choice in _OPTIONS[key]}


def _user(conn, dataset_id: str, user_id: str):
    if not isinstance(dataset_id, str) or not dataset_id or not isinstance(user_id, str) or not user_id:
        raise ValueError("Select a dataset and user before accessing private preferences.")
    row = conn.execute("""SELECT u.name FROM users u JOIN datasets d USING(dataset_id)
        WHERE u.dataset_id=? AND u.user_id=?""", (dataset_id, user_id)).fetchone()
    if row is None:
        raise ValueError("The selected user does not belong to this dataset.")
    return row


def _decoded(value: str) -> dict:
    try:
        return _known_preferences(json.loads(value))
    except (json.JSONDecodeError, TypeError):
        return {}


def load_preferences(db_path: str | Path, dataset_id: str, user_id: str) -> dict:
    """Return allowed enums only; another user's chats and preferences stay private."""
    with closing(connect(db_path)) as conn:
        _user(conn, dataset_id, user_id)
        row = conn.execute("""SELECT preferences_json FROM user_preferences
            WHERE dataset_id=? AND user_id=?""", (dataset_id, user_id)).fetchone()
        return _decoded(row["preferences_json"]) if row else {}


def save_preferences(db_path: str | Path, dataset_id: str, user_id: str, *,
                     well_sort: str | None = None, cost_display: str | None = None,
                     source_message_id: int | None = None) -> dict:
    """Save explicit formatting choices without changing shared operational facts.

    The server supplies provenance; a referenced message must be a real user
    message in this same dataset and user scope. Conversation IDs are omitted
    deliberately so preferences survive the user's next conversation.
    """
    updates = {key: choice for key, choice in {"well_sort": well_sort, "cost_display": cost_display}.items()
               if choice is not None}
    if not updates:
        raise ValueError("Provide a well_sort or cost_display preference to save.")
    for key, choice in updates.items():
        if not isinstance(choice, str) or choice not in _OPTIONS[key]:
            raise ValueError(f"Invalid {key}; use one of: {', '.join(sorted(_OPTIONS[key]))}.")
    if source_message_id is not None and (type(source_message_id) is not int or source_message_id < 1):
        raise ValueError("source_message_id must identify a saved user message.")

    with closing(connect(db_path)) as conn, conn:
        # Serialize read/merge/write so simultaneous partial updates do not erase
        # a different formatting choice saved by another chat of this same user.
        conn.execute("BEGIN IMMEDIATE")
        user = _user(conn, dataset_id, user_id)
        if source_message_id is not None:
            source = conn.execute("""SELECT message_id FROM chat_messages WHERE message_id=?
                AND dataset_id=? AND user_id=? AND role='user'""",
                (source_message_id, dataset_id, user_id)).fetchone()
            if source is None:
                raise ValueError("Preference provenance must be this user's own message in this dataset.")
        row = conn.execute("""SELECT preferences_json FROM user_preferences
            WHERE dataset_id=? AND user_id=?""", (dataset_id, user_id)).fetchone()
        preferences = _decoded(row["preferences_json"]) if row else {}
        preferences.update(updates)
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        conn.execute("""INSERT INTO user_preferences
            (dataset_id,user_id,preferences_json,updated_at,source_message_id) VALUES (?,?,?,?,?)
            ON CONFLICT(dataset_id,user_id) DO UPDATE SET
                preferences_json=excluded.preferences_json,updated_at=excluded.updated_at,
                source_message_id=excluded.source_message_id""",
            (dataset_id, user_id, json.dumps(preferences, sort_keys=True), updated_at, source_message_id))

    citation = {"type": "private_user_preference", "table": "user_preferences", "record_id": user_id,
                "scope": "private", "user_id": user_id, "user_name": user["name"], "updated_at": updated_at}
    if source_message_id is not None:
        citation["source_message_id"] = source_message_id
    return {"saved": True, "scope": "private", "user_name": user["name"], "preferences": preferences,
            "updated_at": updated_at, "citations": [citation]}
