"""Local Streamlit interface for the field inspection copilot."""

from __future__ import annotations

from contextlib import closing
import json
import re
from datetime import date
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st

from copilot import db
from copilot.agent import Copilot
from copilot.config import Settings
from copilot.ingest import import_dataset, import_zip
from copilot.previews import PREVIEW_LABELS, WELL_PREVIEWS, WINDOW_PREVIEWS, build_preview
from copilot.tools import ToolService


BROWSER_TABLES = (
    "wells", "inspections", "context_notes", "maintenance", "regulatory_events",
    "equipment", "documents", "users", "teams", "areas",
)
TABLE_LABELS = {
    "wells": "Wells", "inspections": "Inspections", "context_notes": "Shared context",
    "maintenance": "Maintenance", "regulatory_events": "Regulatory events",
    "equipment": "Equipment", "documents": "Source documents", "users": "Crew",
    "teams": "Teams", "areas": "Areas",
}


def _rows(db_path: str | Path, sql: str, params: tuple = ()) -> list[dict]:
    with closing(db.connect(db_path)) as connection:
        return [dict(row) for row in connection.execute(sql, params)]


def _json(value, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _safe_error(error: Exception) -> str:
    """Do not echo credentials even if an upstream exception includes a URL."""
    message = str(error)
    message = re.sub(r"(?i)(?:sk-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]+)", "[redacted]", message)
    message = re.sub(r"(?i)(key|api_key|authorization)=([^&\s]+)", r"\1=[redacted]", message)
    return message[:1500] or type(error).__name__


def _existing_tables(db_path: str | Path) -> set[str]:
    return {row["name"] for row in _rows(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db_path: str | Path, table: str) -> list[str]:
    if table not in BROWSER_TABLES:
        return []
    return [row["name"] for row in _rows(db_path, f'PRAGMA table_info("{table}")')]


def _start_new_chat(scope: str) -> None:
    session_id = str(uuid4())
    st.session_state[f"active_session:{scope}"] = session_id
    st.session_state[f"conversation:{scope}"] = session_id


def _select_imported(result: dict) -> None:
    # The dataset widget is already mounted during an import. Apply its next
    # value at the start of the following run, before Streamlit creates it.
    st.session_state["next_dataset_choice"] = result["dataset_id"]
    action = "Already imported" if result.get("reused") else "Imported"
    st.session_state["import_notice"] = f"{action}: {result.get('label') or result['dataset_id']}"


def _import_controls(settings: Settings, dataset_map: dict | None = None) -> str | None:
    selected = None
    with st.sidebar.expander("Import dataset", expanded=True):
        if st.session_state.get("import_notice"):
            st.info(st.session_state.pop("import_notice"))
        if dataset_map:
            selected = st.selectbox("Dataset", list(dataset_map), key="dataset_choice", format_func=lambda value: dataset_map[value]["label"])
        st.caption("Import a ZIP or local folder. Existing datasets remain available.")
        label = st.text_input("Dataset label", placeholder="Optional, for example Evaluation data")
        uploaded = st.file_uploader("Dataset ZIP", type=["zip"])
        if st.button("Import ZIP", disabled=uploaded is None, width="stretch"):
            try:
                with st.spinner("Validating and importing dataset…"):
                    result = import_zip(uploaded.getvalue(), settings.db_path, label=label or uploaded.name)
                    _select_imported(result)
                st.rerun()
            except Exception as error:
                st.error(f"Import failed. {_safe_error(error)}")
        local_path = st.text_input("Local dataset folder or ZIP", value=str(settings.dataset_path))
        if st.button("Import local data", width="stretch"):
            try:
                with st.spinner("Validating and importing dataset…"):
                    source = Path(local_path).expanduser()
                    result = import_zip(source.read_bytes(), settings.db_path, label=label or None) if source.suffix.lower() == ".zip" else import_dataset(source, settings.db_path, label=label or None)
                    _select_imported(result)
                st.rerun()
            except Exception as error:
                st.error(f"Import failed. {_safe_error(error)}")
    return selected


def _render_evidence(citations: list[dict], tool_calls: list | None = None) -> None:
    if citations:
        with st.expander(f"Evidence · {len(citations)} source{'s' if len(citations) != 1 else ''}"):
            for number, source in enumerate(citations, 1):
                if not isinstance(source, dict):
                    st.write(str(source))
                    continue
                label = source.get("label") or source.get("title") or source.get("record_id") or f"Source {number}"
                st.markdown(f"**{number}. {label}**")
                fields = []
                for name in ("table", "record_id", "source_path", "path", "source_line", "line", "line_start", "line_end", "author_user_id", "created_at", "session_id", "feedback_kind"):
                    if source.get(name) is not None:
                        fields.append(f"{name.replace('_', ' ')}: {source[name]}")
                if fields:
                    st.caption(" · ".join(fields))
                excerpt = source.get("content") or source.get("snippet") or source.get("excerpt")
                if excerpt:
                    st.write(excerpt)
                elif not fields:
                    st.json(source)
    if tool_calls:
        with st.expander("Query details", expanded=False):
            for call in tool_calls:
                if isinstance(call, dict):
                    st.json({key: value for key, value in call.items() if key not in {"api_key", "authorization"}})
                else:
                    st.write(str(call))


def _render_visuals(visuals: list[dict]) -> None:
    for visual in visuals or []:
        if not isinstance(visual, dict):
            continue
        try:
            kind = str(visual.get("kind", "table")).removesuffix("_chart")
            if visual.get("title"):
                st.caption(visual["title"])
            frame = pd.DataFrame(visual.get("data") or [])
            if frame.empty:
                st.caption("No matching records for this view.")
                continue
            x, y = visual.get("x"), visual.get("y")
            if kind == "status":
                for record in frame.to_dict("records"):
                    with st.container(border=True):
                        st.markdown(f"**{record.get('asset_id', 'Well status')}**")
                        production = record.get("is_producing")
                        known = production is not None and not pd.isna(production)
                        status = ("Producing" if bool(production) else "Not producing") if known else "Unknown"
                        status_col, delta_col, threshold_col = st.columns(3)
                        status_col.metric("Inspection-based status", status)
                        for column, label, field in (
                            (delta_col, "Wellhead − ambient", "temperature_delta_c"),
                            (threshold_col, "Manual threshold", "production_delta_c"),
                        ):
                            value = record.get(field)
                            column.metric(label, f"{float(value):g} °C" if value is not None and not pd.isna(value) else "Unknown")
                        facts = []
                        if record.get("inspected_at"):
                            facts.append(f"Observed {record['inspected_at']}")
                        if record.get("inspector_name"):
                            facts.append(f"Inspector {record['inspector_name']}")
                        if record.get("area_name"):
                            facts.append(str(record["area_name"]))
                        for label, field in (("Wellhead", "wellhead_temp_c"), ("Ambient", "ambient_temp_c")):
                            value = record.get(field)
                            if value is not None and not pd.isna(value):
                                facts.append(f"{label} {float(value):g} °C")
                        st.caption(" · ".join(facts))
                        st.caption("Latest available inspection evidence; this is not a live sensor reading.")
            elif kind in {"table", "dataframe"}:
                st.dataframe(frame, hide_index=True, width="stretch")
            elif kind == "bar":
                st.bar_chart(frame, x=x, y=y, width="stretch")
            elif kind == "line":
                st.line_chart(frame, x=x, y=y, width="stretch")
            elif kind == "scatter":
                st.scatter_chart(frame, x=x, y=y, width="stretch")
            elif kind == "map":
                frame = frame.rename(columns={"latitude": "lat", "longitude": "lon", "lng": "lon"})
                st.map(frame, latitude=visual.get("latitude", "lat"), longitude=visual.get("longitude", "lon"))
            elif kind == "heatmap":
                import plotly.express as px

                z = visual.get("z") or visual.get("value")
                matrix = frame.pivot_table(index=y, columns=x, values=z, aggfunc="sum")
                chart = px.imshow(matrix, aspect="auto", color_continuous_scale="Teal")
                st.plotly_chart(chart, width="stretch")
            else:
                st.dataframe(frame, hide_index=True, width="stretch")
        except Exception as error:
            st.caption(f"This view could not be displayed: {_safe_error(error)}")


def _render_token_usage(metrics: list[dict]) -> None:
    records = [item for item in metrics or [] if isinstance(item, dict)]
    if not records:
        return

    def known(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    latest = records[-1].get("input_tokens")
    measured = [item["input_tokens"] for item in records if known(item.get("input_tokens"))]
    latest_column, peak_column, calls_column, sum_column = st.columns(4)
    latest_column.metric("Latest input context", f"{latest:,} tokens" if known(latest) else "Unavailable")
    peak_column.metric("Peak measured context", f"{max(measured):,} tokens" if measured else "Unavailable")
    calls_column.metric("Model rounds", str(len(records)))
    sum_value = f"{sum(measured):,} tokens" if measured else "Unavailable"
    if measured and len(measured) < len(records):
        sum_value += " + unknown"
    sum_column.metric("Input across rounds", sum_value)
    with st.expander("Context token details"):
        st.caption("Input context is the size of one model request, not the sum across rounds. It includes system instructions, database schema, private history, tool definitions and tool results. SOP text is included when retrieved.")
        st.caption("Actual token counts are known only when OpenRouter returns usage. Missing usage is unavailable, never zero. Input across rounds sums returned counts; it is not a live quota meter or a billing total and excludes unknown/repeated failed HTTP attempts.")
        display = []
        for index, item in enumerate(records, 1):
            display.append({"Call": index, "Provider": item.get("provider"), "Model": item.get("model"),
                            "Input tokens": item.get("input_tokens"), "Count source": item.get("input_token_source", "unavailable"),
                            "Input estimate (not measured)": item.get("input_token_estimate"),
                            "Input size (UTF-8 bytes)": item.get("input_size_bytes"),
                            "Output tokens": item.get("output_tokens"), "Total tokens": item.get("total_tokens"),
                            "Generation HTTP attempts": item.get("inference_http_attempts"),
                            "Status": item.get("status"), "Failure": item.get("failure_kind")})
        st.dataframe(pd.DataFrame(display), hide_index=True, width="stretch")
        st.caption("The local request-size estimate uses four UTF-8 bytes per token, not a model tokenizer. It can over- or underestimate the actual context; OpenRouter enforces the model's real context limit. At most three model rounds run per question, with bounded HTTP retries inside a round.")
        output = [item["output_tokens"] for item in records if known(item.get("output_tokens"))]
        if output:
            st.caption(f"Known output usage: {sum(output):,} tokens across {len(output)} of {len(records)} calls. Token counts alone do not establish billed cost.")
        else:
            st.caption("Output usage is unavailable until returned by OpenRouter.")


def _render_message(message: dict) -> None:
    role = message.get("role", "assistant")
    if role not in {"user", "assistant"}:
        return
    with st.chat_message(role, avatar="🧑‍🔧" if role == "user" else "🤖"):
        st.markdown(message.get("content") or "")
        if role == "assistant":
            payload = _json(message.get("payload_json"), {})
            if payload.get("write_status") == "pending_confirmation" and any(
                    item.get("mode") == "validated explicit correction" for item in payload.get("tool_calls", [])):
                st.caption("Prepared from your explicit correction · 0 model calls · awaiting your confirmation.")
            if payload.get("write_status") == "not_saved" and payload.get("provider_error"):
                st.error(payload["provider_error"])
            _render_token_usage(payload.get("token_usage", []))
            _render_visuals(payload.get("visuals", []))
            _render_evidence(payload.get("citations", []), payload.get("tool_calls"))


def _pending_controls(copilot: Copilot, pending: dict | None, session_id: str, pending_key: str) -> None:
    if not pending:
        return
    with st.container(border=True):
        st.markdown("**Review shared update**")
        st.caption("Confirmed updates become available to other crew members in this dataset. The original records remain preserved.")
        preview = pending.get("preview") or pending.get("arguments") or {}
        if not isinstance(preview, dict):
            preview = {}
        if preview.get("asset_id"):
            st.markdown(f"**Well:** {preview['asset_id']}")
        if preview.get("inspection_id"):
            st.markdown(f"**Inspection:** {preview['inspection_id']}")
        is_correction = pending.get("tool") == "correct_inspection" or "field" in preview
        if is_correction:
            names = {
                "wellhead_temp_c": "Wellhead temperature (°C)",
                "ambient_temp_c": "Ambient temperature (°C)",
                "spill_detected": "Spill detected",
                "spill_area_pct": "Spill area (%)",
                "spill_est_volume_bbl": "Estimated spill volume (bbl)",
            }
            field = preview.get("field", "Inspection value")
            st.markdown(f"**Measurement:** {names.get(field, field)}")
            if preview.get("feedback_kind"):
                feedback_labels = {"sensor_reading_correction": "Sensor reading correction",
                                   "ai_detection_feedback": "AI detection feedback",
                                   "ai_estimate_feedback": "AI estimate feedback"}
                st.caption("Feedback type: " + feedback_labels.get(preview["feedback_kind"], preview["feedback_kind"]))
            old_value = preview.get("current_value", preview.get("original_value"))
            new_value = preview.get("new_value", preview.get("value"))
            def display_value(value):
                if value is None:
                    return "Unknown"
                if isinstance(value, bool):
                    return "Yes" if value else "No"
                return str(value)
            before, after = st.columns(2)
            before.metric("Original recorded value", display_value(old_value))
            after.metric("Proposed value", display_value(new_value))
            if preview.get("reason"):
                st.markdown("**Reason**")
                st.write(preview["reason"])
            if preview.get("original_inspector_notes"):
                st.info("Original inspection note (preserved): " + str(preview["original_inspector_notes"]))
        else:
            if preview.get("note_type"):
                st.caption(str(preview["note_type"]).replace("_", " ").title())
            if preview.get("text"):
                st.write(preview["text"])
            if pending.get("tool") == "record_equipment_replacement":
                st.markdown("**Reported replacement date**")
                st.write(preview.get("valid_from"))
                st.caption("Evidence: " + ("Secondhand report — remains unconfirmed" if preview.get("evidence_basis") == "hearsay" else "Direct report from the selected crew member"))
                if preview.get("claimed_source_name"):
                    st.markdown("**Claimed source:** " + str(preview["claimed_source_name"]))
            elif preview.get("valid_from") or preview.get("valid_to"):
                starts, ends = st.columns(2)
                starts.markdown("**Starts**")
                starts.write(preview.get("valid_from") or "Not specified")
                ends.markdown("**Ends (inclusive)**")
                ends.write(preview.get("valid_to") or "Not specified")
                st.caption("An end date does not confirm completed maintenance or resumed production.")
        if pending.get("message"):
            st.info(pending["message"])
        if preview.get("author"):
            st.caption("Correction/update attributed to " + str(preview["author"]) + ".")
        confirm, cancel = st.columns(2)
        if confirm.button("Confirm shared update", key=f"confirm:{pending_key}", type="primary", width="stretch"):
            try:
                with st.spinner("Saving shared context…"):
                    copilot.confirm_update(pending, session_id)
                st.session_state[pending_key] = None
                st.session_state["chat_notice"] = "Shared update saved. Other crew members can retrieve it now."
                st.rerun()
            except Exception as error:
                st.error(f"The update was not saved. {_safe_error(error)}")
        if cancel.button("Cancel update", key=f"cancel:{pending_key}", width="stretch"):
            try:
                copilot.cancel_update(pending, session_id)
                st.session_state[pending_key] = None
                st.session_state["chat_notice"] = "Proposed update cancelled. No shared record was added."
                st.rerun()
            except Exception as error:
                st.error(f"The proposal could not be cancelled. {_safe_error(error)}")


def _chat(settings: Settings, dataset: dict, user_id: str, session_id: str) -> None:
    copilot = Copilot(settings.db_path, dataset["dataset_id"], user_id, settings)
    messages = _rows(
        settings.db_path,
        "SELECT * FROM chat_messages WHERE dataset_id=? AND user_id=? AND session_id=? ORDER BY created_at, rowid",
        (dataset["dataset_id"], user_id, session_id),
    )
    pending_key = f"pending:{dataset['dataset_id']}:{user_id}:{session_id}"
    if pending_key not in st.session_state:
        last_assistant = next((item for item in reversed(messages) if item.get("role") == "assistant"), None)
        st.session_state[pending_key] = _json(last_assistant.get("payload_json"), {}).get("pending_update") if last_assistant else None
    if st.session_state.get("chat_notice"):
        st.success(st.session_state.pop("chat_notice"))

    prompt = None
    if not messages:
        st.markdown("### Your inspection records, connected.")
        st.write("Ask about wells, inspections, repairs, or operating procedures. Report a crew update to review it before sharing.")
        starter_prompts = [
            "Give me an overview of this field and the latest inspections.",
            "Which wells have the most downtime this year? Show a chart.",
            "What does the SOP say about correcting a false spill detection?",
            "Show recent shared crew notes and any conflicting reports.",
        ]
        columns = st.columns(2)
        for index, starter in enumerate(starter_prompts):
            if columns[index % 2].button(starter, key=f"starter:{index}", width="stretch"):
                prompt = starter
    else:
        for message in messages:
            _render_message(message)

    _pending_controls(copilot, st.session_state.get(pending_key), session_id, pending_key)
    if not settings.api_key:
        st.info("Set OPENROUTER_API_KEY in the local .env file and reload the app to enable chat. Dataset import and the data browser remain available.")
    entered = st.chat_input("Ask the field copilot, or report a well update…")
    prompt = entered or prompt
    if prompt:
        if not settings.api_key:
            st.warning("Configure the API key in the local .env file, reload the app, and send your message again.")
            return
        transient_message = st.empty()
        with transient_message.container():
            with st.chat_message("user", avatar="🧑‍🔧"):
                st.markdown(prompt)
        token_placeholder = st.empty()

        def show_usage(metrics):
            with token_placeholder.container():
                _render_token_usage(metrics)

        try:
            with st.spinner("Checking field records and shared context…"):
                result = copilot.respond(prompt, session_id, usage_callback=show_usage)
            st.session_state[pending_key] = result.get("pending_update")
            transient_message.empty()
            token_placeholder.empty()
            st.rerun()
        except Exception:
            st.error("The copilot could not complete this request. Check the provider configuration in .env and available quota, then retry.")


def _data_browser(settings: Settings, dataset_id: str) -> None:
    st.markdown("### Explore the imported data")
    available = _existing_tables(settings.db_path)
    options = [table for table in BROWSER_TABLES if table in available]
    if not options:
        st.info("No data tables are available yet.")
        return
    left, right = st.columns([1, 2])
    table = left.selectbox("Records", options, format_func=lambda value: TABLE_LABELS.get(value, value))
    columns = _columns(settings.db_path, table)
    query = right.text_input("Filter by well, record ID, or name", placeholder="Optional text filter")
    if "dataset_id" not in columns:
        st.info("This source is not available for dataset browsing.")
        return
    where = "dataset_id=?"
    params: list = [dataset_id]
    searchable = [name for name in columns if name.endswith("_id") or name in {"name", "title", "content", "body", "text", "note", "source_path"}]
    if query and searchable:
        where += " AND (" + " OR ".join(f'CAST("{name}" AS TEXT) LIKE ?' for name in searchable) + ")"
        params.extend([f"%{query}%"] * len(searchable))
    count = _rows(settings.db_path, f'SELECT COUNT(*) AS count FROM "{table}" WHERE {where}', tuple(params))[0]["count"]
    records = _rows(settings.db_path, f'SELECT * FROM "{table}" WHERE {where} LIMIT 200', tuple(params))
    st.caption(f"{count:,} matching records · showing up to 200 · dataset {dataset_id}")
    frame = pd.DataFrame(records)
    hidden = [name for name in ("dataset_id", "raw_json", "content_hash", "file_hash") if name in frame.columns]
    st.dataframe(frame.drop(columns=hidden), hide_index=True, width="stretch")
    if records:
        st.download_button(
            "Download these rows as CSV", frame.drop(columns=hidden).to_csv(index=False).encode("utf-8"),
            file_name=f"{table}-{dataset_id[:8]}.csv", mime="text/csv",
        )
        if "raw_json" in frame.columns:
            with st.expander("Original record example"):
                st.json(_json(records[0].get("raw_json"), {}))
        lat = next((name for name in ("lat", "latitude") if name in frame.columns), None)
        lon = next((name for name in ("lon", "longitude", "lng") if name in frame.columns), None)
        if lat and lon:
            mapped = frame[[lat, lon]].rename(columns={lat: "lat", lon: "lon"}).dropna()
            if not mapped.empty:
                st.map(mapped)


def _visual_preview(settings: Settings, dataset_id: str, user_id: str) -> None:
    st.markdown("### Database preview: no model API calls")
    st.write("Check the actual charts and cards with preset database queries. These previews do not test the copilot's natural-language planning or save a conversation.")
    kind = st.selectbox("Preview", list(PREVIEW_LABELS), format_func=PREVIEW_LABELS.get, key="visual_preview_kind")
    asset_id = None
    if kind in WELL_PREVIEWS:
        wells = _rows(settings.db_path, "SELECT asset_id FROM wells WHERE dataset_id=? ORDER BY asset_id", (dataset_id,))
        if not wells:
            st.info("No wells are available in this dataset.")
            return
        asset_id = st.selectbox("Preview well", [item["asset_id"] for item in wells], key=f"preview_well:{dataset_id}")
    end = date.fromisoformat(settings.as_of_date)
    start = end.replace(month=1, day=1)
    if kind in WINDOW_PREVIEWS:
        left, right = st.columns(2)
        start = left.date_input("Preview starts", value=start, max_value=end, key=f"preview_start:{dataset_id}")
        end = right.date_input("Preview ends (inclusive)", value=end, max_value=end, key=f"preview_end:{dataset_id}")
    signature = (kind, asset_id, start.isoformat(), end.isoformat(), settings.as_of_date)
    key = f"visual_preview:{dataset_id}:{user_id}"
    if st.button("Show preview", type="primary"):
        try:
            with st.spinner("Reading local inspection records…"):
                tools = ToolService(settings.db_path, dataset_id, user_id, settings.as_of_date, field_timezone=settings.field_timezone)
                preview = build_preview(tools, kind, asset_id=asset_id, start_date=start.isoformat(), end_date=end.isoformat())
            st.session_state[key] = (signature, preview)
        except Exception as error:
            st.session_state.pop(key, None)
            st.error(f"The database preview could not be prepared. {_safe_error(error)}")
    saved = st.session_state.get(key)
    if saved and saved[0] == signature:
        preview = saved[1]
        st.caption(preview["description"])
        if preview["date_range"]:
            st.caption("Inspection window: " + " through ".join(preview["date_range"]) + " (inclusive)")
        for warning in preview["warnings"]:
            st.caption(warning)
        _render_visuals(preview["visuals"])
        _render_evidence(preview["citations"], preview["tool_calls"])
    else:
        st.caption("Choose a view and select Show preview. Only that button runs the preview query.")


def main() -> None:
    st.set_page_config(page_title="Field Intelligence · Inspection copilot", page_icon="🛢️", layout="wide")
    st.markdown(
        """<style>
        .stApp {background: #f7faf9;}
        [data-testid="stSidebar"] {background: #edf3f1;}
        h1, h2, h3 {color: #123d36;}
        div[data-testid="stMetric"] {background: white; padding: .7rem 1rem; border: 1px solid #dce8e3; border-radius: .65rem;}
        [data-testid="stChatMessage"] {border: 1px solid #e2ebe6; border-radius: .7rem;}
        .block-container {padding-top: 2rem; padding-bottom: 2rem; max-width: 1320px;}
        </style>""", unsafe_allow_html=True,
    )
    settings = Settings.from_env()
    db.initialize(settings.db_path)
    datasets = _rows(settings.db_path, "SELECT * FROM datasets ORDER BY imported_at DESC, dataset_id")
    if not datasets and "initial_import_attempted" not in st.session_state:
        st.session_state["initial_import_attempted"] = True
        try:
            with st.spinner("Preparing the sample field dataset for the first run…"):
                import_dataset(settings.dataset_path, settings.db_path, label="Sample field")
            datasets = _rows(settings.db_path, "SELECT * FROM datasets ORDER BY imported_at DESC, dataset_id")
        except Exception as error:
            st.session_state["import_notice"] = f"Sample import needs attention: {_safe_error(error)}"

    if not datasets:
        _import_controls(settings)
        st.title("Field Intelligence")
        st.info("Import the challenge dataset using the sidebar to begin.")
        st.stop()

    dataset_map = {row["dataset_id"]: row for row in datasets}
    configured_source = str(Path(settings.dataset_path).resolve())
    if st.session_state.get("configured_dataset_source") != configured_source:
        configured_dataset = next((row["dataset_id"] for row in datasets
                                   if row.get("source_path") and str(Path(row["source_path"]).resolve()) == configured_source), None)
        if configured_dataset:
            st.session_state["next_dataset_choice"] = configured_dataset
            st.session_state["configured_dataset_source"] = configured_source
    if st.session_state.get("next_dataset_choice") in dataset_map:
        st.session_state["dataset_choice"] = st.session_state.pop("next_dataset_choice")
    if st.session_state.get("dataset_choice") not in dataset_map:
        st.session_state["dataset_choice"] = datasets[0]["dataset_id"]
    dataset_id = _import_controls(settings, dataset_map)
    dataset = dataset_map[dataset_id]
    st.title("Field Intelligence")
    try:
        analysis_date = date.fromisoformat(str(settings.as_of_date)).strftime("%d %b %Y")
    except ValueError:
        analysis_date = str(settings.as_of_date)
    st.caption(f"{dataset['label']} · Analysis as of {analysis_date} · {settings.field_timezone}")
    users = _rows(settings.db_path, "SELECT user_id, name, role, team_id FROM users WHERE dataset_id=? ORDER BY name", (dataset_id,))
    if not users:
        st.error("The selected dataset has no crew identities. Import a complete dataset to start a conversation.")
        st.stop()
    users_by_id = {user["user_id"]: user for user in users}
    crew_control, conversation_control, new_chat_control = st.columns([1.3, 2, 1], vertical_alignment="bottom")
    user_id = crew_control.selectbox(
        "User / crew member", list(users_by_id), key=f"crew:{dataset_id}",
        format_func=lambda value: f"{users_by_id[value]['name']} · {value}",
        help="Demo identity from the selected dataset. Switch users to see their private conversations and the same shared well notes.",
    )
    user = users_by_id[user_id]

    scope = f"{dataset_id}:{user_id}"
    active_key = f"active_session:{scope}"
    if active_key not in st.session_state:
        st.session_state[active_key] = str(uuid4())
    conversations = _rows(
        settings.db_path,
        "SELECT session_id, MIN(created_at) AS first_at, MAX(created_at) AS last_at FROM chat_messages WHERE dataset_id=? AND user_id=? GROUP BY session_id ORDER BY last_at DESC LIMIT 50",
        (dataset_id, user_id),
    )
    labels = {}
    for item in conversations:
        first = _rows(
            settings.db_path,
            "SELECT content FROM chat_messages WHERE dataset_id=? AND user_id=? AND session_id=? AND role='user' ORDER BY created_at, rowid LIMIT 1",
            (dataset_id, user_id, item["session_id"]),
        )
        title = first[0]["content"] if first else "Conversation"
        labels[item["session_id"]] = title[:60] + ("…" if len(title) > 60 else "")
    active = st.session_state[active_key]
    if active not in labels:
        labels = {active: "New conversation", **labels}
    widget_key = f"conversation:{scope}"
    if st.session_state.get(widget_key) not in labels:
        st.session_state[widget_key] = active
    session_id = conversation_control.selectbox("Conversation", list(labels), key=widget_key, format_func=lambda value: labels[value])
    st.session_state[active_key] = session_id
    new_chat_control.button("＋ New conversation", on_click=_start_new_chat, args=(scope,), width="stretch")
    st.caption(f"Active user: {user['name']} ({user_id}) · {user['role'].replace('_', ' ').title()} · {user['team_id']} · Private chats · Shared well notes")
    counts = _json(dataset.get("counts_json"), {})
    metrics = st.columns(4)
    available_tables = _existing_tables(settings.db_path)
    for column, table, label in zip(metrics, ("wells", "inspections", "context_notes", "documents"), ("Wells", "Inspections", "Shared notes", "Documents")):
        count = None if table == "context_notes" else counts.get(table)
        if count is None and table in available_tables:
            count = _rows(settings.db_path, f'SELECT COUNT(*) AS count FROM "{table}" WHERE dataset_id=?', (dataset_id,))[0]["count"]
        column.metric(label, f"{int(count or 0):,}")
    chat_tab, preview_tab, data_tab = st.tabs(["✦ Copilot", "Visual preview", "Data browser"])
    with chat_tab:
        _chat(settings, dataset, user_id, session_id)
    with data_tab:
        _data_browser(settings, dataset_id)
    with preview_tab:
        _visual_preview(settings, dataset_id, user_id)


if __name__ == "__main__":
    main()
