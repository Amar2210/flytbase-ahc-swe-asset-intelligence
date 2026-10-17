"""Validated, atomic and additive ingestion of the challenge's folder or ZIP data."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import shutil
import stat
import uuid
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .db import connect, initialize

MAX_ZIP_FILES = 30_000
MAX_ZIP_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024


class DatasetValidationError(ValueError):
    """The dataset cannot be imported without losing or misjoining evidence."""


@contextmanager
def _scratch_directory(parent: Path):
    # tempfile.mkdtemp uses Windows ACL restrictions incompatible with some
    # sandboxed demo hosts. Ordinary mkdir inherits the workspace's usable ACL.
    parent = parent.resolve()
    folder = parent / ("asset-copilot-import-" + uuid.uuid4().hex)
    folder.mkdir()
    try:
        yield folder
    finally:
        if folder.resolve().parent != parent or not folder.name.startswith("asset-copilot-import-"):
            raise DatasetValidationError("Refusing cleanup outside the import workspace.")
        shutil.rmtree(folder)


def _root(path: str | Path) -> Path:
    path = Path(path).resolve()
    if not path.is_dir():
        raise DatasetValidationError(f"Dataset directory does not exist: {path}")
    if (path / "fleet/areas.json").is_file() and (path / "assets").is_dir():
        return path
    roots = [p.parent.parent for p in path.rglob("areas.json")
             if p.parent.name == "fleet" and (p.parent.parent / "assets").is_dir()]
    if len(roots) != 1:
        raise DatasetValidationError("Select one dataset folder containing fleet/ and assets/ (or its parent).")
    return roots[0]


def _text(path: Path) -> str:
    if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
        raise DatasetValidationError(f"Unsafe or oversized dataset file: {path.name}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise DatasetValidationError(f"Dataset text is not UTF-8: {path}") from exc


def _json(path: Path):
    try:
        return json.loads(_text(path), parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    except ValueError as exc:
        raise DatasetValidationError(f"Invalid JSON in {path}: {exc}") from exc


def _rows(path: Path):
    if not path.is_file():
        raise DatasetValidationError(f"Missing required data file: {path}")
    for line_no, line in enumerate(_text(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
        except ValueError as exc:
            raise DatasetValidationError(f"Invalid JSON in {path.name}:{line_no}: {exc}") from exc
        if not isinstance(record, dict):
            raise DatasetValidationError(f"Expected an object in {path.name}:{line_no}")
        yield record, line_no


def _required(record: dict, key: str, location: str):
    value = record.get(key)
    if value is None or value == "":
        raise DatasetValidationError(f"Missing {key} in {location}")
    return value


def _date(value, location: str, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise DatasetValidationError(f"Invalid date in {location}: {value!r}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetValidationError(f"Invalid date in {location}: {value!r}") from exc


def _number(value, location, nullable=False):
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DatasetValidationError(f"Expected a finite number in {location}: {value!r}")


def _threshold(body: str, path: str):
    # Only a manual's explicitly labelled production-temperature section is authoritative.
    match = re.search(r"(?im)^#{1,6}\s+[^\n]*production temperature rule[^\n]*\n(?P<body>.*?)(?=^#{1,6}\s|\Z)", body, re.S | re.M)
    if not match:
        raise DatasetValidationError(f"No production temperature rule section in {path}")
    section = match.group("body")
    clean = re.sub(r"[*_`]+", "", section)
    values = re.findall(r"at least\s+(\d+(?:\.\d+)?)\s*°?\s*C\s+above\s+ambient", clean, re.I)
    if len(set(values)) != 1:
        raise DatasetValidationError(f"Missing or ambiguous production threshold in {path}")
    quote = " ".join(section.split())
    line = body[:match.start("body")].count("\n") + 1
    return float(values[0]), quote, line


def import_dataset(source_path: str | Path, db_path: str | Path, label: str | None = None) -> dict:
    root = _root(source_path)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if any(p.is_symlink() or root not in p.resolve().parents for p in files):
        raise DatasetValidationError("Dataset links may not escape the selected folder.")
    fingerprint = hashlib.sha256()
    for file in files:
        if file.stat().st_size > MAX_FILE_BYTES:
            raise DatasetValidationError(f"Oversized file: {file.name}")
        fingerprint.update(file.relative_to(root).as_posix().encode())
        fingerprint.update(b"\0")
        fingerprint.update(file.read_bytes())
        fingerprint.update(b"\0")
    dataset_id = "ds_" + fingerprint.hexdigest()[:24]
    initialize(db_path)
    conn = connect(db_path)
    try:
        existing = conn.execute("SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
        if existing:
            return {"dataset_id": dataset_id, "label": existing["label"], "counts": json.loads(existing["counts_json"]), "reused": True}
        conn.execute("BEGIN IMMEDIATE")
        dataset_label = label or root.name
        conn.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?)", (dataset_id, dataset_label, str(root), datetime.now(timezone.utc).isoformat(), None, "{}"))
        counts = {name: 0 for name in ("teams", "users", "areas", "documents", "well_types", "wells", "equipment", "inspections", "maintenance", "regulatory_events", "context_notes")}
        refs = {"areas": {}, "teams": {}, "users": {}, "wells": {}, "inspections": {}}
        table_columns = {table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                         for table in ("inspections", "maintenance", "regulatory_events", "context_notes")}
        pending_inspection_refs = []
        pending_user_refs = []
        pending_protocol_refs = []
        last_inspections = []

        def add(table, raw, source, line, values, key):
            rec_id = _required(raw, key, source)
            if not isinstance(rec_id, str):
                raise DatasetValidationError(f"ID {key} must be text in {source}")
            row = {"dataset_id": dataset_id, key: rec_id, **values,
                   "raw_json": json.dumps(raw, ensure_ascii=False, allow_nan=False),
                   "source_path": source, "source_line": line}
            columns = ",".join(row)
            try:
                conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
            except Exception as exc:
                raise DatasetValidationError(f"Invalid or duplicate {table} record {rec_id} at {source}:{line}: {exc}") from exc
            counts[table] = counts.get(table, 0) + 1
            if table in refs:
                refs[table][rec_id] = raw

        areas_path = root / "fleet/areas.json"
        area_data = _json(areas_path)
        user_data = _json(root / "fleet/users.json")
        for table, records, source, key, keys in [
            ("teams", area_data.get("teams", []), "fleet/areas.json", "team_id", ["name", "lead_user_id"]),
            ("users", user_data.get("users", []), "fleet/users.json", "user_id", ["name", "role", "team_id"]),
            ("areas", area_data.get("areas", []), "fleet/areas.json", "area_id", ["name", "team_id"]),
        ]:
            if not isinstance(records, list) or not records:
                raise DatasetValidationError(f"No valid {table} list in {source}")
            for raw in records:
                values = {k: _required(raw, k, source) for k in keys}
                if table == "areas":
                    center = raw.get("center", {})
                    values.update(center_lat=center.get("lat"), center_lon=center.get("lon"))
                add(table, raw, source, None, values, key)
        docs = {}
        protocol_ids = set()
        for file in files:
            if file.suffix.lower() != ".md":
                continue
            relative = file.relative_to(root).as_posix()
            parts = file.relative_to(root).parts
            kind = "manual" if parts[:2] == ("fleet", "well-types") else "sop" if parts[:2] == ("fleet", "sops") else "report"
            asset_id = parts[1] if parts[0] == "assets" and len(parts) > 2 else None
            body = _text(file)
            if kind == "sop":
                protocol_ids.update(re.findall(r"\b[A-Z]{2}-\d{2}\b", body))
            document_id = "doc_" + hashlib.sha256(relative.encode()).hexdigest()[:24]
            title = next((line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("#")), file.stem)
            conn.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)", (dataset_id, document_id, relative, kind, asset_id, file.stem if kind == "manual" else None, title, body))
            docs[relative] = document_id
            counts["documents"] = counts.get("documents", 0) + 1
            try:
                conn.execute("INSERT INTO document_fts(dataset_id,document_id,title,body) VALUES(?,?,?,?)", (dataset_id, document_id, title, body))
            except Exception as exc:
                if "no such table" not in str(exc):
                    raise
            if kind == "manual":
                threshold, quote, line = _threshold(body, relative)
                conn.execute("INSERT INTO well_types VALUES(?,?,?,?,?,?)", (dataset_id, file.stem, threshold, document_id, quote, line))
                counts["well_types"] = counts.get("well_types", 0) + 1
        latest_date = None
        for asset_dir in sorted((root / "assets").iterdir()):
            if not asset_dir.is_dir():
                continue
            metadata_path = asset_dir / "metadata.json"
            if not metadata_path.is_file():
                raise DatasetValidationError(f"Missing metadata.json in {asset_dir.name}")
            raw = _json(metadata_path)
            asset_id = _required(raw, "asset_id", str(metadata_path))
            if asset_id != asset_dir.name:
                raise DatasetValidationError(f"Asset ID does not match its folder: {asset_dir.name}")
            source = metadata_path.relative_to(root).as_posix()
            values = {key: _required(raw, key, source) for key in ("name", "asset_category", "well_type", "area_id", "team_id", "in_charge_user_id", "installed_on", "recorded_status")}
            _date(values["installed_on"], source)
            values.update(latitude=raw.get("location", {}).get("lat"), longitude=raw.get("location", {}).get("lon"))
            for key in ("latitude", "longitude"):
                _number(values[key], f"{source}:{key}")
            add("wells", raw, source, None, values, "asset_id")
            pending_user_refs.append((values["in_charge_user_id"], source))
            if raw.get("last_inspection"):
                last_inspections.append((asset_id, raw["last_inspection"], source))
            if not conn.execute("SELECT 1 FROM well_types WHERE dataset_id=? AND well_type=?", (dataset_id, values["well_type"])).fetchone():
                raise DatasetValidationError(f"No manual and production rule for well type {values['well_type']}")
            for equip in raw.get("equipment", []):
                add("equipment", equip, source, None, {"asset_id": asset_id, "kind": _required(equip, "kind", source), "model": equip.get("model")}, "equipment_id")
            for filename, table, key in (("inspections.jsonl", "inspections", "inspection_id"), ("maintenance.jsonl", "maintenance", "maintenance_id"), ("regulatory-events.jsonl", "regulatory_events", "event_id"), ("context.jsonl", "context_notes", "note_id")):
                file = asset_dir / filename
                relative = file.relative_to(root).as_posix()
                for record, line in _rows(file):
                    if record.get("asset_id") != asset_id:
                        raise DatasetValidationError(f"Cross-well asset_id in {relative}:{line}")
                    values = dict(record)
                    values.pop(key, None)
                    if table == "inspections":
                        for field in ("inspected_at", "inspector_user_id", "method", "ambient_temp_c", "wellhead_temp_c", "spill_detected"):
                            _required(record, field, f"{relative}:{line}")
                        _date(record["inspected_at"], relative)
                        for field in ("ambient_temp_c", "wellhead_temp_c", "spill_area_pct", "spill_est_volume_bbl"):
                            _number(record.get(field), f"{relative}:{line}:{field}", nullable=field.startswith("spill_"))
                        if not isinstance(record["spill_detected"], bool):
                            raise DatasetValidationError(f"spill_detected must be boolean in {relative}:{line}")
                        values["spill_detected"] = int(record["spill_detected"])
                        values["evidence_images_json"] = json.dumps(values.pop("evidence_images", []))
                        pending_user_refs.append((record["inspector_user_id"], relative))
                        latest_date = max(latest_date or record["inspected_at"][:10], record["inspected_at"][:10])
                    elif table == "maintenance":
                        for field in ("maintenance_type", "equipment_kind", "technician_user_id"):
                            _required(record, field, relative)
                        for field in ("started_at", "completed_at"):
                            _date(_required(record, field, relative), relative)
                        if record["completed_at"] < record["started_at"]:
                            raise DatasetValidationError(f"Repair ends before it starts in {relative}:{line}")
                        costs = values.pop("cost_usd", {})
                        values.update(cost_labor=costs.get("labor"), cost_parts=costs.get("parts"), cost_total=costs.get("total"))
                        for field in ("cost_labor", "cost_parts", "cost_total", "downtime_hours"):
                            _number(values.get(field), f"{relative}:{line}:{field}")
                        pending_user_refs.append((record.get("technician_user_id"), relative))
                        if record.get("triggered_by_inspection_id"):
                            pending_inspection_refs.append((record["triggered_by_inspection_id"], asset_id, relative))
                        if record.get("report_file"):
                            report = PurePosixPath(str(record["report_file"]).replace("\\", "/"))
                            if report.is_absolute() or ".." in report.parts or ":" in str(report):
                                raise DatasetValidationError(f"Unsafe report_file in {relative}:{line}")
                            # Dataset report_file is relative to its well folder.
                            report_path = (asset_dir / Path(*report.parts)).relative_to(root).as_posix()
                            if report_path not in docs:
                                # Also accept a dataset-root path in same-format test sets.
                                report_path = report.as_posix()
                            if report_path not in docs or not report_path.startswith(f"assets/{asset_id}/"):
                                raise DatasetValidationError(f"Missing or cross-well report {record['report_file']} in {relative}:{line}")
                    elif table == "regulatory_events":
                        _date(_required(record, "date", relative), relative)
                        for field in ("agency", "event_type", "protocol_id", "status"):
                            _required(record, field, relative)
                        pending_protocol_refs.append((record["protocol_id"], relative))
                        if record.get("related_inspection_id"):
                            pending_inspection_refs.append((record["related_inspection_id"], asset_id, relative))
                    else:
                        for field in ("author_user_id", "note_type", "text"):
                            _required(record, field, relative)
                        for field in ("created_at", "valid_from"):
                            _date(_required(record, field, relative), relative)
                        _date(record.get("valid_to"), relative, nullable=True)
                        if record.get("valid_to") and record["valid_to"] < record["valid_from"]:
                            raise DatasetValidationError(f"Context validity ends before it starts in {relative}:{line}")
                        values["origin"] = "dataset"
                        pending_user_refs.append((record.get("author_user_id"), relative))
                        if record.get("note_type") in {"correction", "data_correction"} and not record.get("inspection_id"):
                            raise DatasetValidationError(f"Correction requires inspection_id in {relative}:{line}")
                        if record.get("inspection_id"):
                            pending_inspection_refs.append((record["inspection_id"], asset_id, relative))
                    # Unknown source fields remain in raw_json for lossless provenance.
                    values = {k: v for k, v in values.items() if k in table_columns[table]}
                    add(table, record, relative, line, values, key)
        if not refs["wells"] or not refs["inspections"]:
            raise DatasetValidationError("Dataset must contain wells and inspections.")
        for user in refs["users"].values():
            if user["team_id"] not in refs["teams"]:
                raise DatasetValidationError(f"Unknown team {user['team_id']} for {user['user_id']}")
        for team in refs["teams"].values():
            pending_user_refs.append((team["lead_user_id"], "fleet/areas.json"))
        for user_id, source in pending_user_refs:
            if user_id not in refs["users"]:
                raise DatasetValidationError(f"Unknown user {user_id!r} in {source}")
        for area in refs["areas"].values():
            if area["team_id"] not in refs["teams"]:
                raise DatasetValidationError(f"Unknown team for area {area['area_id']}")
            for asset_id in area.get("asset_ids", []):
                well = refs["wells"].get(asset_id)
                if not well or well["area_id"] != area["area_id"]:
                    raise DatasetValidationError(f"Area membership mismatch for {asset_id}")
        for well in refs["wells"].values():
            area = refs["areas"].get(well["area_id"])
            # A transferred well can retain its geographic area while its own
            # team takes priority over the area's default production team.
            if not area or well["team_id"] not in refs["teams"] or well["asset_id"] not in area.get("asset_ids", []):
                raise DatasetValidationError(f"Invalid area/team membership for {well['asset_id']}")
        for inspection_id, asset_id, source in pending_inspection_refs:
            inspection = refs["inspections"].get(inspection_id)
            if not inspection or inspection["asset_id"] != asset_id:
                raise DatasetValidationError(f"Missing or cross-well inspection {inspection_id} in {source}")
        for protocol_id, source in pending_protocol_refs:
            if protocol_id not in protocol_ids:
                raise DatasetValidationError(f"Unknown SOP protocol {protocol_id} in {source}")
        for asset_id, last, source in last_inspections:
            inspection = refs["inspections"].get(last.get("inspection_id"))
            if not inspection or inspection["asset_id"] != asset_id:
                raise DatasetValidationError(f"Invalid last_inspection reference in {source}")
        # The benchmark date is explicit in the guide. Standalone uploads default to
        # their latest observation and can be overridden in the UI.
        as_of_date = latest_date
        guide = root.parent / "02-dataset-guide.md"
        if guide.is_file():
            match = re.search(r"Treat\s+\*\*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\*\*\s+as\s+[\"“]today", _text(guide))
            if match:
                as_of_date = datetime.strptime(match[1], "%d %b %Y").date().isoformat()
        # The released test field is nested under test-data/dataset and declares
        # its benchmark date in the adjacent release notes, not the sample guide.
        release_notes = root.parent / "README.md"
        if release_notes.is_file():
            match = re.search(r'["“]Today["”]\s+is\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})', _text(release_notes), re.I)
            if match:
                as_of_date = datetime.strptime(match[1], "%d %b %Y").date().isoformat()
        conn.execute("UPDATE datasets SET as_of_date=?, counts_json=? WHERE dataset_id=?", (as_of_date, json.dumps(counts), dataset_id))
        conn.commit()
        return {"dataset_id": dataset_id, "label": dataset_label, "counts": counts, "reused": False}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def import_zip(upload_bytes: bytes, db_path: str | Path, label: str | None = None) -> dict:
    if len(upload_bytes) > MAX_ZIP_BYTES:
        raise DatasetValidationError("ZIP exceeds the 512 MiB compressed-size limit.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(upload_bytes))
    except zipfile.BadZipFile as exc:
        raise DatasetValidationError("Upload is not a valid ZIP archive.") from exc
    extraction_parent = Path(db_path).resolve().parent
    extraction_parent.mkdir(parents=True, exist_ok=True)
    with archive, _scratch_directory(extraction_parent) as temp:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_FILES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise DatasetValidationError("ZIP exceeds the file-count or expanded-size limit.")
        seen = set()
        root = Path(temp).resolve()
        for info in infos:
            name = info.filename.replace("\\", "/")
            relative = PurePosixPath(name)
            if (relative.is_absolute() or ".." in relative.parts or ":" in name or "\x00" in name
                    or stat.S_ISLNK(info.external_attr >> 16) or info.file_size > MAX_FILE_BYTES):
                raise DatasetValidationError(f"Unsafe ZIP entry: {name}")
            if name.casefold() in seen:
                raise DatasetValidationError(f"Duplicate ZIP entry: {name}")
            seen.add(name.casefold())
            target = root / Path(*relative.parts)
            if root != target.resolve() and root not in target.resolve().parents:
                raise DatasetValidationError(f"ZIP path escapes extraction directory: {name}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    total = 0
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_FILE_BYTES:
                            raise DatasetValidationError(f"Oversized ZIP entry: {name}")
                        output.write(chunk)
        result = import_dataset(root, db_path, label=label or "Uploaded dataset")
        # Source evidence is persisted; don't expose an ephemeral extraction path.
        if not result["reused"]:
            with closing(connect(db_path)) as conn, conn:
                conn.execute("UPDATE datasets SET source_path=? WHERE dataset_id=?", ("zip-upload", result["dataset_id"]))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="Dataset folder, repository root, or ZIP")
    parser.add_argument("--db", default="data/copilot.sqlite3")
    parser.add_argument("--label")
    args = parser.parse_args()
    try:
        path = Path(args.path)
        result = import_zip(path.read_bytes(), args.db, args.label) if path.suffix.lower() == ".zip" else import_dataset(path, args.db, args.label)
    except (DatasetValidationError, OSError) as exc:
        parser.exit(1, f"Import failed: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
