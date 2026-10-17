"""Pump recall evidence and reviewed, attributed replacement reports.

Installation facts come from imported reports, not generic equipment labels. A
runtime report is a separate claim; it never edits the maintenance source rows.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import re
from uuid import uuid4


def _source(dataset_id, table, row, *, line=None):
    key = {"documents": "document_id", "maintenance": "maintenance_id",
           "equipment_replacement_reports": "report_id", "context_notes": "note_id"}[table]
    result = {"dataset_id": dataset_id, "table": table, "record_id": row[key]}
    for name in ("path", "source_path", "source_line", "created_at", "author_user_id"):
        if row.get(name) is not None:
            result[name] = row[name]
    if line is not None:
        result.update(line_start=line, line_end=line)
    if table == "equipment_replacement_reports":
        result.update(source_path="runtime:equipment-replacement", author_user_id=row["reported_by_user_id"])
    return result


def parse_recall_notice(document):
    """Parse the notice's positive lot list, excluding its examples of safe lots."""
    lines = document["body"].splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.search(r"\bpumps?\s+from\s+(?:these|the following)\s+(?:manufacturing\s+)?lots\s*:", line, re.I)), None)
    if start is None:
        return None
    heading = lines[start]
    model = re.search(r"\b(?!LOT-)([A-Z][A-Z0-9]*-\d[\w.-]*)\b", heading)
    if not model:
        return None
    lots, lot_lines = [], []
    for i in range(start + 1, len(lines)):
        line = lines[i].strip()
        if not line and not lots:
            continue
        match = re.match(r"[-*]\s+`?(LOT-[A-Za-z0-9-]+)`?\s*$", line)
        if match:
            lots.append(match[1].upper())
            lot_lines.append(i + 1)
        elif lots:
            break
    if not lots:
        return None
    notice_date = None
    date_match = re.search(r"\*\*Date:\*\*\s*(.+)", document["body"])
    if date_match:
        for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
            try:
                notice_date = datetime.strptime(date_match[1].strip(), fmt).date().isoformat()
                break
            except ValueError:
                continue
    reference = re.search(r"\*\*Reference:\*\*\s*(.+)", document["body"])
    product_prefix = heading[:model.start()].strip()
    return {"reference": reference[1].strip() if reference else document["path"],
            "model": model[1], "product_prefix": product_prefix, "affected_lots": lots,
            "notice_date": notice_date, "document": document,
            "line_start": start + 1, "line_end": max(lot_lines)}


def parse_parts_installed(body):
    """Accept explicit installed-parts lines, preserving exact report line numbers."""
    parsed = []
    for line_number, line in enumerate(body.splitlines(), 1):
        match = re.search(r"\*\*Parts installed:\*\*\s*(?P<product>.+?)\s+pump,\s*"
                          r"serial\s+(?P<serial>[\w-]+),\s*lot\s+(?P<lot>LOT-[\w-]+)", line, re.I)
        if match:
            parsed.append({"product": match["product"].strip(), "serial": match["serial"],
                           "lot": match["lot"].upper(), "line": line_number})
    return parsed


def _load_installations(service, conn):
    notices = []
    documents = {}
    for row in conn.execute("SELECT * FROM documents WHERE dataset_id=? AND (body LIKE '%Parts installed:%' OR lower(body) LIKE '%recall%')", (service.dataset_id,)):
        doc = dict(row)
        documents[doc["path"]] = doc
        rule = parse_recall_notice(doc)
        if rule and (not rule["notice_date"] or rule["notice_date"] <= service.as_of_date):
            notices.append(rule)
    installations, pending = {}, []
    for row in conn.execute("""SELECT m.*,w.area_id,w.team_id,a.name area_name
        FROM maintenance m JOIN wells w ON w.dataset_id=m.dataset_id AND w.asset_id=m.asset_id
        LEFT JOIN areas a ON a.dataset_id=w.dataset_id AND a.area_id=w.area_id
        WHERE m.dataset_id=? AND m.equipment_kind='downhole_pump'
          AND NOT EXISTS(SELECT 1 FROM equipment e WHERE e.dataset_id=m.dataset_id
                         AND e.asset_id=m.asset_id AND e.kind='esp_pump')
          AND date(m.started_at)<=? AND m.report_file IS NOT NULL""", (service.dataset_id, service.as_of_date)):
        job = dict(row)
        relative = str(job["report_file"]).replace("\\", "/")
        doc = documents.get(f"assets/{job['asset_id']}/{relative}") or documents.get(relative)
        if not doc:
            continue
        for part in parse_parts_installed(doc["body"]):
            item = {**part, "job": job, "document": doc, "asset_id": job["asset_id"]}
            if not job["completed_at"] or job["completed_at"][:10] > service.as_of_date:
                pending.append(item)
                continue
            old = installations.get(job["asset_id"])
            # Source-file order and IDs do not establish chronology.
            if old is None or (job["completed_at"], job["started_at"], job["maintenance_id"]) > (
                    old["job"]["completed_at"], old["job"]["started_at"], old["job"]["maintenance_id"]):
                installations[job["asset_id"]] = item
    return notices, installations, pending


def _matches(installation, rule):
    product = installation["product"].casefold()
    return (installation["lot"] in rule["affected_lots"]
            and re.search(r"(?<![\w-])" + re.escape(rule["model"]) + r"(?![\w-])", installation["product"], re.I)
            and (not rule["product_prefix"] or rule["product_prefix"].casefold() in product))


def _reports(service, conn):
    return [dict(row) for row in conn.execute("""SELECT r.*,u.name reporter_name,s.name claimed_source_name
        FROM equipment_replacement_reports r
        JOIN users u ON u.dataset_id=r.dataset_id AND u.user_id=r.reported_by_user_id
        LEFT JOIN users s ON s.dataset_id=r.dataset_id AND s.user_id=r.claimed_source_user_id
        WHERE r.dataset_id=? AND r.effective_on<=? ORDER BY r.effective_on,r.created_at,r.report_id""",
                                            (service.dataset_id, service.as_of_date))]


def get_recalled_pumps(service, my_area=False, include_install_cost=False):
    if not isinstance(my_area, bool) or not isinstance(include_install_cost, bool):
        raise ValueError("my_area and include_install_cost must be booleans.")
    from copilot.preferences import load_preferences
    preferences = load_preferences(service.db_path, service.dataset_id, service.user_id)
    with service._connect() as conn:
        service._check_session(conn)
        selected = conn.execute("SELECT name,team_id FROM users WHERE dataset_id=? AND user_id=?", (service.dataset_id, service.user_id)).fetchone()
        notices, installations, pending = _load_installations(service, conn)
        if not notices:
            raise ValueError("No supported, effective supplier recall notice was found in the selected dataset.")
        reports = _reports(service, conn)
        rows, excluded, unconfirmed, citations = [], [], [], []
        if preferences:
            pref = conn.execute("SELECT updated_at,source_message_id FROM user_preferences WHERE dataset_id=? AND user_id=?", (service.dataset_id, service.user_id)).fetchone()
            if pref:
                citation = {"type": "private_user_preference", "table": "user_preferences",
                            "record_id": service.user_id, "scope": "private", "user_id": service.user_id,
                            "user_name": selected["name"], "updated_at": pref["updated_at"]}
                if pref["source_message_id"] is not None:
                    citation["source_message_id"] = pref["source_message_id"]
                citations.append(citation)
        for rule in notices:
            citation = _source(service.dataset_id, "documents", rule["document"])
            citation.update(line_start=rule["line_start"], line_end=rule["line_end"])
            citations.append(citation)
        for asset_id, installation in installations.items():
            job = installation["job"]
            if my_area and job["team_id"] != selected["team_id"]:
                continue
            matching = [rule for rule in notices if _matches(installation, rule)]
            if not matching:
                continue
            applicable = [report for report in reports if report["asset_id"] == asset_id
                          and report["effective_on"] >= job["completed_at"][:10]]
            direct = [report for report in applicable if report["status"] == "reported_replaced"
                      and report["evidence_basis"] == "direct_report"]
            hearsay = [report for report in applicable if report["status"] == "unconfirmed"
                       or report["evidence_basis"] == "hearsay"]
            for report in applicable:
                citations.append(_source(service.dataset_id, "equipment_replacement_reports", report))
            if direct:
                report = direct[-1]
                excluded.append({"asset_id": asset_id, "effective_on": report["effective_on"],
                                 "reported_by": report["reporter_name"], "report_id": report["report_id"],
                                 "status": report["status"], "evidence_basis": report["evidence_basis"],
                                 "basis": "Reported replaced by the named user; work order not independently verified."})
                continue
            source_path = installation["document"]["path"]
            row = {"asset_id": asset_id, "area": job["area_name"] or job["area_id"],
                   "pump": installation["product"], "serial": installation["serial"], "lot": installation["lot"],
                   "installed_on": job["completed_at"][:10], "installation_job": job["maintenance_id"],
                   "status": "Replacement unconfirmed" if hearsay else "No later replacement recorded",
                   "source": f"{source_path}:{installation['line']}"}
            ongoing = [item for item in pending if item["asset_id"] == asset_id
                       and item["job"]["started_at"] >= job["completed_at"]]
            if ongoing:
                interim = max(ongoing, key=lambda item: item["job"]["started_at"])
                row["status"] = "Installation in progress; current pump unconfirmed"
                row["in_progress_job"] = interim["job"]["maintenance_id"]
                citations.extend([_source(service.dataset_id, "documents", interim["document"], line=interim["line"]),
                                  _source(service.dataset_id, "maintenance", interim["job"])])
            if include_install_cost:
                thousands = preferences.get("cost_display") == "thousands_usd"
                row["installation_cost_usd_thousands" if thousands else "installation_cost_usd"] = (
                    round(job["cost_total"] / 1000, 3) if thousands and job["cost_total"] is not None else job["cost_total"])
            rows.append(row)
            for report in hearsay:
                unconfirmed.append({"asset_id": asset_id, "reported_by": report["reporter_name"],
                                    "claimed_source": report["claimed_source_name"], "statement": report["statement"],
                                    "report_id": report["report_id"], "effective_on": report["effective_on"],
                                    "status": report["status"], "evidence_basis": report["evidence_basis"],
                                    "effect": "Retained on recall list pending direct confirmation or work order."})
            citations.extend([_source(service.dataset_id, "documents", installation["document"], line=installation["line"]),
                              _source(service.dataset_id, "maintenance", job)])
        rows.sort(key=(lambda row: (row["area"], row["asset_id"])) if preferences.get("well_sort") == "area_then_well_id" else (lambda row: row["asset_id"]))
        return {"rows": rows, "returned_rows": len(rows), "total_rows": len(rows), "truncated": False,
                "as_of_date": service.as_of_date, "scope": "selected user's current production team" if my_area else "whole field",
                "scope_team_id": selected["team_id"] if my_area else None,
                "recall_rules": [{key: value for key, value in rule.items() if key not in {"document", "line_start", "line_end"}} for rule in notices],
                "excluded_reported_replacements": excluded, "unconfirmed_reports": unconfirmed,
                "preferences_applied": preferences,
                "installation_cost_basis": "Total cost of the installation maintenance job, not just the pump part price." if include_install_cost else None,
                "coverage": {"completed_pump_installations_found": len(installations),
                             "in_progress_installation_reports": len(pending)},
                "warning": "Current pump is inferred from the latest completed installation record. Interim jobs with future planned finishes do not prove a replacement is complete. User-reported replacements are attributed claims, not invented work orders; hearsay does not remove a pump.",
                "citations": citations}


def preview_equipment_replacement(service, arguments):
    from copilot.tools import normalize_asset_id
    asset_id = normalize_asset_id(arguments.get("asset_id", ""))
    effective = date.fromisoformat(arguments.get("effective_on", "")).isoformat()
    if effective > service.as_of_date:
        raise ValueError("A completed replacement report cannot be dated after the dataset evaluation date.")
    statement = service._valid_text(arguments.get("statement"), "Replacement report")
    basis = arguments.get("evidence_basis")
    if basis not in {"direct_report", "hearsay"}:
        raise ValueError("Evidence basis must be direct_report or hearsay.")
    claimed = arguments.get("claimed_source_name")
    with service._connect() as conn:
        service._check_session(conn)
        well = conn.execute("SELECT name FROM wells WHERE dataset_id=? AND asset_id=?", (service.dataset_id, asset_id)).fetchone()
        if well is None:
            raise ValueError("Well does not belong to the selected dataset.")
        user = conn.execute("SELECT name FROM users WHERE dataset_id=? AND user_id=?", (service.dataset_id, service.user_id)).fetchone()
        source = None
        if claimed:
            claimed = service._valid_text(claimed, "Claimed source", 200)
            sources = conn.execute("SELECT user_id,name FROM users WHERE dataset_id=? AND lower(name)=lower(?)", (service.dataset_id, claimed)).fetchall()
            if len(sources) != 1:
                raise ValueError("Claimed source must identify one exact person in the selected dataset.")
            source = dict(sources[0])
            if source["user_id"] != service.user_id:
                basis = "hearsay"  # A named third party cannot become the current reporter.
        normalized = {"asset_id": asset_id, "effective_on": effective, "statement": statement,
                      "evidence_basis": basis}
        if source:
            normalized["claimed_source_name"] = source["name"]
        return {"arguments": normalized, "asset_id": asset_id, "well_name": well["name"],
                "text": statement, "valid_from": effective, "evidence_basis": basis,
                "claimed_source_name": source["name"] if source else None,
                "reported_by": user["name"], "author": user["name"],
                "warning": ("Indirect report: saved as unconfirmed and retained on the recall list. Obtain direct confirmation or a work order."
                            if basis == "hearsay" else "Reported replacement: excludes this well's earlier recalled installation after confirmation. Original records remain unchanged.")}


def record_equipment_replacement(service, **arguments):
    preview = preview_equipment_replacement(service, arguments)
    normalized = preview["arguments"]
    created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with service._connect() as conn:
        service._check_session(conn)
        claimed = conn.execute("SELECT user_id FROM users WHERE dataset_id=? AND name=?", (service.dataset_id, normalized.get("claimed_source_name"))).fetchone()
        report = {"report_id": "EQR-" + uuid4().hex[:16], "dataset_id": service.dataset_id,
                  "asset_id": normalized["asset_id"], "reported_by_user_id": service.user_id,
                  "claimed_source_user_id": claimed[0] if claimed else None,
                  "evidence_basis": normalized["evidence_basis"],
                  "status": "unconfirmed" if normalized["evidence_basis"] == "hearsay" else "reported_replaced",
                  "effective_on": normalized["effective_on"], "statement": normalized["statement"],
                  "created_at": created, "source_note_id": "CTX-LIVE-" + uuid4().hex[:16]}
        note = {"dataset_id": service.dataset_id, "note_id": report["source_note_id"],
                "asset_id": normalized["asset_id"], "created_at": created, "author_user_id": service.user_id,
                "note_type": "operator_note", "inspection_id": None,
                "text": f"Pump replacement report ({report['status']}; {report['evidence_basis']}): {normalized['statement']}",
                "valid_from": normalized["effective_on"], "valid_to": None, "origin": "runtime",
                "source_path": "runtime:equipment-replacement", "source_line": None}
        note["raw_json"] = json.dumps({key: value for key, value in note.items() if key != "dataset_id"})
        conn.execute(f"INSERT INTO context_notes ({','.join(note)}) VALUES ({','.join('?' for _ in note)})", tuple(note.values()))
        conn.execute(f"INSERT INTO equipment_replacement_reports ({','.join(report)}) VALUES ({','.join('?' for _ in report)})", tuple(report.values()))
    return {"saved": True, "scope": "shared", "report": report,
            "author": preview["reported_by"], "claimed_source_name": preview["claimed_source_name"],
            "warning": preview["warning"], "citations": [_source(service.dataset_id, "equipment_replacement_reports", report),
                                                         _source(service.dataset_id, "context_notes", note)]}
