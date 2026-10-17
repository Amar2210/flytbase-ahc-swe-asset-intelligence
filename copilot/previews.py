"""Explicit database visual previews, independent of model inference and chat."""

from __future__ import annotations

from datetime import date

from copilot.tools import ToolService, normalize_asset_id


PREVIEW_LABELS = {
    "status": "Well status card",
    "trend": "Well temperature trend",
    "downtime": "Top 10 wells by production downtime",
    "spills": "Corrected spills by well type and month",
    "map": "Well locations map",
    "table": "Effective inspection table",
}
WELL_PREVIEWS = {"status", "trend", "table"}
WINDOW_PREVIEWS = {"trend", "downtime", "spills", "table"}


def build_preview(tools: ToolService, kind: str, *, asset_id: str | None = None,
                  start_date: str | None = None, end_date: str | None = None) -> dict:
    """Execute a known, read-only query and bind visual data to its real rows.

    The caller must explicitly request a preview. This is a preset query path,
    not a natural-language answer or a substitute for successful model testing.
    """
    if kind not in PREVIEW_LABELS:
        raise ValueError("Choose an available database preview.")
    asset = normalize_asset_id(asset_id) if kind in WELL_PREVIEWS else None
    end = date.fromisoformat(end_date or tools.as_of_date)
    start = date.fromisoformat(start_date or f"{end.year}-01-01")
    if kind in WINDOW_PREVIEWS and (start > end or end > date.fromisoformat(tools.as_of_date)):
        raise ValueError("Use a date range ending on or before the configured analysis date.")

    if kind == "status":
        tool, arguments = "get_well", {"asset_id": asset}
    else:
        window = f"date(i.inspected_at) BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
        if kind == "trend":
            sql = ("SELECT i.inspection_id,i.inspected_at,"
                   "i.effective_wellhead_temp_c AS wellhead_c,i.effective_ambient_temp_c AS ambient_c "
                   f"FROM effective_inspections i WHERE i.asset_id='{asset}' AND {window} "
                   "ORDER BY i.inspected_at,i.inspection_id")
        elif kind == "downtime":
            sql = ("SELECT i.asset_id,7*COUNT(*) AS downtime_days FROM effective_inspections i "
                   f"WHERE i.is_producing=0 AND {window} GROUP BY i.asset_id "
                   "ORDER BY downtime_days DESC,i.asset_id LIMIT 10")
        elif kind == "spills":
            sql = ("SELECT w.well_type,strftime('%Y-%m',i.inspected_at) AS month,"
                   "COUNT(*) AS spill_count FROM effective_inspections i JOIN wells w USING(asset_id) "
                   f"WHERE i.effective_spill_detected=1 AND {window} "
                   "GROUP BY w.well_type,month ORDER BY month,w.well_type")
        elif kind == "map":
            sql = "SELECT asset_id,name,well_type,latitude,longitude FROM wells ORDER BY asset_id"
        else:
            sql = ("SELECT i.inspection_id,i.asset_id,i.inspected_at,i.effective_wellhead_temp_c,"
                   "i.effective_ambient_temp_c,i.temperature_delta_c,i.production_delta_c,i.is_producing,"
                   "i.effective_spill_detected,i.effective_spill_est_volume_bbl,i.correction_sources,"
                   "i.has_disputed_corrections FROM effective_inspections i "
                   f"WHERE i.asset_id='{asset}' AND {window} ORDER BY i.inspected_at,i.inspection_id")
        tool, arguments = "query_records", {"sql": sql}
    result = tools.execute(tool, arguments)
    if result.get("error"):
        raise ValueError(result["error"])

    warnings = []
    if kind == "status":
        well, latest = result["well"], result.get("latest_inspection")
        rows = [{**latest, "asset_id": well["asset_id"], "area_name": well.get("area_name"),
                 "wellhead_temp_c": latest.get("effective_wellhead_temp_c"),
                 "ambient_temp_c": latest.get("effective_ambient_temp_c")} ] if latest else []
        visual = {"kind": "status"}
        warnings = result.get("warnings", [])
        description = f"Latest available inspection on or before {tools.as_of_date}; accepted corrections and manual threshold applied."
    else:
        rows = result.get("rows", [])
        visual = {
            "trend": {"kind": "line", "x": "inspected_at", "y": ["wellhead_c", "ambient_c"]},
            "downtime": {"kind": "bar", "x": "asset_id", "y": "downtime_days"},
            "spills": {"kind": "heatmap", "x": "month", "y": "well_type", "z": "spill_count"},
            "map": {"kind": "map"}, "table": {"kind": "table"},
        }[kind]
        if kind == "downtime":
            description = "Seven days per non-producing weekly inspection; missed inspections add zero. Repair duration is a separate measure."
        elif kind == "spills":
            description = "Accepted false-positive corrections are excluded; each counted row is a spill detection at an inspection."
        elif kind == "map":
            description = "Imported well coordinates. Map background tiles may use network access; no model API is called."
        elif kind == "trend":
            description = "Wellhead and ambient temperatures in °C, using effective corrected readings."
        else:
            description = "Effective readings and correction references; original source inspections remain preserved."
        if result.get("truncated"):
            warnings.append(f"The preview reached the {result['returned_rows']}-row output cap; further rows were omitted. Narrow the date range for a complete visual.")
    return {"title": PREVIEW_LABELS[kind], "description": description,
            "date_range": [start.isoformat(), end.isoformat()] if kind in WINDOW_PREVIEWS else None,
            "visuals": [{**visual, "title": PREVIEW_LABELS[kind], "data": rows}],
            "citations": result.get("citations", []), "warnings": warnings,
            "tool_calls": [{"tool": tool, "arguments": arguments, "status": "completed", "mode": "database preview; no model API calls"}]}
