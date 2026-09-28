import base64
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import smtplib
import sys
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from flask import Blueprint, jsonify, request, send_file
from werkzeug.utils import secure_filename
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

try:
    import requests
except ImportError:  # pragma: no cover - requests should always be installed via requirements.txt
    requests = None

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
except ImportError:
    PdfReader = PdfWriter = canvas = ImageReader = None

import db


certificate_api = Blueprint("certificate_api", __name__, url_prefix="/api/v1")
ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "public" / "fonts"

def _register_certificate_fonts():
    fonts = {
        "CertCinzel": ("Cinzel-Regular.ttf", "Cinzel-Bold.ttf", "Cinzel-Italic.ttf", "Cinzel-BoldItalic.ttf"),
        "CertPlayfair": ("PlayfairDisplay-Regular.ttf", "PlayfairDisplay-Bold.ttf", "PlayfairDisplay-Italic.ttf", "PlayfairDisplay-BoldItalic.ttf"),
        "CertInter": ("Inter-Regular.ttf", "Inter-Bold.ttf", "Inter-Italic.ttf", "Inter-BoldItalic.ttf"),
        "CertJakarta": ("PlusJakartaSans-Regular.ttf", "PlusJakartaSans-Bold.ttf", "PlusJakartaSans-Italic.ttf", "PlusJakartaSans-BoldItalic.ttf"),
        "CertVibes": ("GreatVibes-Regular.ttf", "GreatVibes-Bold.ttf", "GreatVibes-Italic.ttf", "GreatVibes-BoldItalic.ttf"),
    }
    for family, files in fonts.items():
        for suffix, filename in zip(("", "-Bold", "-Italic", "-BoldItalic"), files):
            name = family + suffix
            path = FONT_DIR / filename
            if path.exists() and name not in pdfmetrics.getRegisteredFontNames():
                try:
                    pdfmetrics.registerFont(TTFont(name, str(path)))
                except Exception:
                    pass

_register_certificate_fonts()

def _now():
    return datetime.now(timezone.utc).isoformat()


def _default_state():
    return {
        "imports": [], "participants": [], "templates": [], "certificates": [],
        "emailJobs": [], "auditLogs": [], "settings": {
            "eventName": "", "organizationName": "", "certificateIdPrefix": "CERT",
            "issueDate": "", "activeTemplateId": "", "requireCheckIn": True,
            "requireCheckOut": True, "senderName": "", "replyToAddress": "",
            "emailSubject": "Your certificate", "emailBodyTemplate": "",
        },
    }


def _load_state():
    try:
        saved = db.load_state() or {}
    except Exception as exc:
        raise RuntimeError(f"Certificate portal data cannot be read: {exc}") from exc
    if not isinstance(saved, dict):
        raise RuntimeError("Certificate portal state is not a dictionary")
    saved_portal = saved.get("certificatePortal") if isinstance(saved.get("certificatePortal"), dict) else saved
    if saved_portal is None:
        return _default_state()
    state = _default_state()
    state.update(saved_portal)
    state["settings"] = {**_default_state()["settings"], **saved_portal.get("settings", {})}
    return state


portal_state = _load_state()

@certificate_api.before_request
def _refresh_portal_state():
    # Vercel may reuse a warm Python process after another instance has changed
    # the database. Reload the authoritative state before every API request so
    # imports, template edits, certificates and email logs survive refreshes and
    # do not remain stuck in a stale worker snapshot.
    global portal_state
    try:
        fresh = _load_state()
        if isinstance(fresh, dict):
            portal_state = fresh
    except Exception:
        # Let the actual endpoint return the normal storage/configuration error.
        pass


def _save_state():
    container = db.load_state() or {}
    if not isinstance(container, dict):
        container = {}
    container["certificatePortal"] = portal_state
    db.save_state(container)


def _response(data=None, message=None, status=200):
    payload = {"success": True, "data": data}
    if message:
        payload["message"] = message
    return jsonify(payload), status


def _error(code, message, status=400, details=None):
    return jsonify(success=False, error={"code": code, "message": message, "details": details or {}},
                   requestId=uuid.uuid4().hex), status


def _token(user):
    body = json.dumps({"sub": user["id"], "role": user["role"], "exp": int(time.time()) + 86400},
                      separators=(",", ":")).encode().hex()
    sig = hmac.new(os.getenv("JWT_SECRET", "certificate-portal-local-secret").encode(),
                   body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _current_admin():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        body, sig = header[7:].split(".", 1)
        expected = hmac.new(os.getenv("JWT_SECRET", "certificate-portal-local-secret").encode(),
                            body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(bytes.fromhex(body))
        return payload if payload["exp"] >= int(time.time()) and payload["role"] == "ADMIN" else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _require_admin():
    return _current_admin() is not None


def _audit(action, record, status="SUCCESS", details=""):
    portal_state["auditLogs"].insert(0, {
        "id": f"aud_{uuid.uuid4().hex[:10]}", "action": action,
        "admin": os.getenv("ADMIN_EMAIL", "administrator"), "record": record,
        "date": _now(), "status": status, "details": details,
    })
    _save_state()


def _normalize_key(value):
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _extract_pdf_rows(raw):
    if PdfReader is None:
        raise ValueError("PDF processing is unavailable. Install the backend PDF dependencies.")
    reader = PdfReader(io.BytesIO(raw))
    lines = []
    for page in reader.pages:
        lines.extend((page.extract_text() or "").splitlines())
    rows = []
    for line in lines:
        values = [part.strip() for part in line.split("|")]
        if len(values) > 1:
            rows.append(values)
    if not rows:
        return [], []
    headers = rows[0]
    return headers, [dict(zip(headers, row)) for row in rows[1:]]


def _decode_csv(raw):
    # CSV exports can be UTF-8, UTF-8 with BOM, UTF-16, or occasionally
    # another common Windows encoding. Decode without inventing/replacing
    # participant data whenever possible.
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("The CSV encoding could not be read. Please save the file as UTF-8 CSV and upload it again.")


def _parse_csv_text(text):
    # Excel/Google Sheets exports may use comma, semicolon, or tab delimiters.
    # Sniff only from the header/sample and fall back to comma.
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    raw_headers = list(reader.fieldnames or [])
    headers = [str(h or "").strip() for h in raw_headers]
    if not headers or not any(headers):
        raise ValueError("The CSV has no readable header row. Make sure the first row contains column names.")
    if len(set(headers)) != len(headers):
        raise ValueError("The CSV contains duplicate column names. Rename the duplicate headers and upload again.")

    rows = []
    for row in reader:
        # Rebuild rows using the cleaned header names so headers such as
        # ` Name ` still map correctly. Extra unnamed cells are ignored rather
        # than silently becoming fabricated fields.
        clean = {}
        for raw_header, header in zip(raw_headers, headers):
            value = row.get(raw_header)
            clean[header] = "" if value is None else str(value).strip()
        if any(value != "" for value in clean.values()):
            rows.append(clean)

    if not rows:
        raise ValueError("The CSV contains headers but no data rows.")
    return headers, rows


def _parse_upload(file_storage):
    filename = secure_filename(file_storage.filename or "")
    extension = Path(filename).suffix.lower()
    raw = file_storage.read()
    if not raw:
        raise ValueError("The uploaded file is empty.")
    if extension == ".csv":
        text = _decode_csv(raw)
        headers, rows = _parse_csv_text(text)
        return headers, rows, "CSV", raw
    if extension == ".pdf":
        headers, rows = _extract_pdf_rows(raw)
        if not headers or not rows:
            raise ValueError("The PDF contains no readable attendance rows.")
        return headers, rows, "PDF", raw
    raise ValueError("Only CSV and PDF attendance files are supported.")


def _mapping_value(row, mapping, key):
    column = mapping.get(key)
    return str(row.get(column, "")).strip() if column else ""


def _participant_from_row(row, mapping, index):
    aliases = {
        "name": ("name", "studentname", "fullname", "participantname"),
        "email": ("email", "mailid", "emailaddress"),
        "studentId": ("studentid", "id", "studentnumber"),
        "rollNumber": ("rollnumber", "rollno", "rollno"),
        "checkIn": ("checkin", "checkintime", "entry", "entrytime"),
        "checkOut": ("checkout", "checkouttime", "exit", "exittime"),
    }
    normalized = {_normalize_key(k): str(v or "").strip() for k, v in row.items()}

    def value(key):
        mapped = _mapping_value(row, mapping, key)
        if mapped:
            return mapped
        for alias in aliases[key]:
            if normalized.get(alias):
                return normalized[alias]
        return ""

    name, email = value("name"), value("email")
    student_id, roll = value("studentId"), value("rollNumber")
    check_in, check_out = value("checkIn"), value("checkOut")
    errors = []
    if not name:
        errors.append("MISSING_NAME")
    if not email or "@" not in email:
        errors.append("INVALID_EMAIL")
    if not student_id:
        errors.append("MISSING_STUDENT_ID")
    if not roll:
        errors.append("MISSING_ROLL_NUMBER")
    if not check_in:
        errors.append("MISSING_CHECK_IN")
    if not check_out:
        errors.append("MISSING_CHECK_OUT")
    return {
        "id": f"part_{uuid.uuid4().hex[:12]}", "name": name, "email": email,
        "studentId": student_id, "rollNumber": roll, "checkIn": check_in or None,
        "checkOut": check_out or None, "eligibility": "PENDING",
        "eligibilityReason": "Awaiting admin eligibility decision",
        "certificateStatus": "PENDING", "validationErrors": errors, "sourceRow": index + 2,
    }


def _paginate(items):
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(max(int(request.args.get("limit", 1000)), 1), 5000)
    total = len(items)
    start = (page - 1) * limit
    return {"items": items[start:start + limit], "pagination": {
        "page": page, "limit": limit, "total": total,
        "totalPages": (total + limit - 1) // limit if total else 0,
    }}


@certificate_api.post("/auth/login")
def portal_login():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or body.get("usernameOrEmail") or "").strip().lower()
    password = str(body.get("password", ""))
    configured_email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
    configured_username = (os.getenv("ADMIN_USERNAME") or "").strip().lower()
    configured_password = os.getenv("ADMIN_PASSWORD") or ""
    if not configured_email or not configured_password:
        return _error("SERVER_NOT_CONFIGURED", "Administrator credentials are not configured on the server.", 503)
    accepted_aliases = {configured_email}
    if configured_username:
        accepted_aliases.add(configured_username)
    if email not in accepted_aliases or not hmac.compare_digest(password, configured_password):
        return _error("INVALID_CREDENTIALS", "Invalid administrator credentials.", 401)
    user = {"id": "admin", "name": os.getenv("ADMIN_FULL_NAME", "Administrator"),
            "email": configured_email, "role": "ADMIN", "lastLogin": _now()}
    return _response({"accessToken": _token(user), "expiresAt": datetime.fromtimestamp(int(time.time()) + 86400, timezone.utc).isoformat(), "user": user})


@certificate_api.get("/auth/me")
def portal_me():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    return _response({"id": "admin", "name": os.getenv("ADMIN_FULL_NAME", "Administrator"),
                      "email": os.getenv("ADMIN_EMAIL", ""), "role": "ADMIN", "lastLogin": _now()})


@certificate_api.post("/auth/logout")
def portal_logout():
    return _response(None, "Logged out successfully")


def _create_import_job(filename, raw, file_type="CSV"):
    """Create and persist an import job from already downloaded CSV/PDF bytes."""
    if not raw:
        raise ValueError("The source file is empty.")
    if len(raw) > 25 * 1024 * 1024:
        raise ValueError("Attendance files are limited to 25 MB.")
    if file_type.upper() == "CSV":
        text = _decode_csv(raw)
        columns, rows = _parse_csv_text(text)
    else:
        columns, rows, _, _ = _parse_upload(type("Upload", (), {"filename": filename, "read": lambda self: raw})())
    import_id = f"imp_{uuid.uuid4().hex[:12]}"
    safe_name = secure_filename(filename) or "google_sheet.csv"
    db.save_file(f"import:{import_id}", safe_name, raw, mimetypes.guess_type(safe_name)[0] or "text/csv")
    job = {"id": import_id, "filename": filename, "fileType": file_type.upper(),
           "sourceType": "GOOGLE_SHEET" if str(filename).startswith("Google Sheet") else "FILE",
           "fileSize": str(len(raw)), "uploadedAt": _now(), "status": "UPLOADED",
           "columns": columns, "rawRows": rows, "mapping": {}, "records": [],
           "totalRecords": len(rows), "validRecords": 0, "invalidRecords": 0,
           "duplicateRecords": 0, "missingNames": 0, "missingEmails": 0,
           "missingIds": 0, "missingRollNumbers": 0, "missingCheckIn": 0, "missingCheckOut": 0}
    portal_state["imports"].append(job)
    _save_state()
    _audit("FILE_UPLOADED", filename)
    return job


@certificate_api.post("/imports")
def create_import():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    uploaded = request.files.get("file")
    if not uploaded:
        return _error("FILE_REQUIRED", "Attendance file is required.", 422)
    raw_size = request.content_length or 0
    if raw_size > 25 * 1024 * 1024:
        return _error("FILE_TOO_LARGE", "Attendance files are limited to 25 MB.", 413)
    try:
        raw = uploaded.read()
        columns, rows, file_type, _ = _parse_upload(type("Upload", (), {"filename": uploaded.filename, "read": lambda self: raw})())
        job = _create_import_job(uploaded.filename, raw, file_type)
        # Keep the exact parsed result from the upload parser.
        job["columns"], job["rawRows"] = columns, rows
        _save_state()
    except ValueError as exc:
        return _error("INVALID_FILE", str(exc), 422)
    return _response({"importId": job["id"], "filename": job["filename"], "fileType": job["fileType"], "status": "UPLOADED"}, status=201)


@certificate_api.post("/imports/google-sheet")
def create_google_sheet_import():
    """Import a public Google Sheet as CSV without storing Google credentials."""
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    body = request.get_json(silent=True) or {}
    source_url = str(body.get("url") or "").strip()
    if not source_url:
        return _error("URL_REQUIRED", "A Google Sheets URL is required.", 422)
    parsed = urlparse(source_url)
    if parsed.scheme not in ("https",) or parsed.netloc.lower() not in {"docs.google.com", "www.docs.google.com"}:
        return _error("INVALID_GOOGLE_SHEET_URL", "Only docs.google.com Google Sheets URLs are supported.", 422)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        sheet_idx = parts.index("d")
        spreadsheet_id = parts[sheet_idx + 1]
    except (ValueError, IndexError):
        return _error("INVALID_GOOGLE_SHEET_URL", "Use a Google Sheets URL containing /spreadsheets/d/<sheet-id>/...", 422)
    qs = parse_qs(parsed.fragment.lstrip("#"))
    gid = qs.get("gid", [None])[0]
    if not gid:
        gid = parse_qs(parsed.query).get("gid", ["0"])[0]
    export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid or '0'}"
    try:
        response = requests.get(export_url, timeout=20, allow_redirects=True, headers={"User-Agent": "CertificatePortal/1.0"})
        if response.status_code != 200:
            return _error("GOOGLE_SHEET_FETCH_FAILED", "Google Sheet could not be downloaded. Make the sheet accessible to anyone with the link and try again.", 422, {"status": response.status_code})
        content_type = (response.headers.get("content-type") or "").lower()
        raw = response.content
        if not raw or ("text/html" in content_type and b"Google" in raw[:5000]):
            return _error("GOOGLE_SHEET_NOT_PUBLIC", "The Google Sheet is not publicly readable. Set General access to anyone with the link as Viewer.", 422)
        # Validate CSV before persisting it.
        columns, rows = _parse_csv_text(_decode_csv(raw))
        if not columns or not rows:
            return _error("GOOGLE_SHEET_EMPTY", "The selected Google Sheet tab contains no readable rows.", 422)
        job = _create_import_job(f"Google Sheet {spreadsheet_id}.csv", raw, "CSV")
        job["sourceType"] = "GOOGLE_SHEET"
        job["sourceUrl"] = source_url
        job["columns"], job["rawRows"] = columns, rows
        _save_state()
        return _response({"importId": job["id"], "filename": job["filename"], "fileType": "CSV", "status": "UPLOADED", "sourceType": "GOOGLE_SHEET", "totalRecords": len(rows)}, status=201)
    except requests.RequestException as exc:
        return _error("GOOGLE_SHEET_FETCH_FAILED", f"Could not reach Google Sheets: {exc}", 422)
    except (UnicodeDecodeError, ValueError) as exc:
        return _error("GOOGLE_SHEET_INVALID", str(exc), 422)


@certificate_api.get("/imports")
def import_history():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    # Never fabricate upload records. This list contains only files actually
    # accepted by the backend and persisted in the portal state.
    items = []
    for job in reversed(portal_state["imports"]):
        items.append({
            key: value for key, value in job.items()
            if key not in ("rawRows", "records", "mapping")
        })
    return _response(_paginate(items))


@certificate_api.get("/imports/<import_id>/file")
def import_file(import_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = _find_import(import_id)
    if not job:
        return _error("NOT_FOUND", "Import job not found.", 404)
    stored = db.load_file(f"import:{import_id}")
    if not stored:
        return _error("NOT_FOUND", "Uploaded file is not available in persistent storage.", 404)
    return send_file(
        io.BytesIO(stored["data"]),
        download_name=stored.get("filename") or job.get("filename") or "attendance-file",
        mimetype=stored.get("content_type") or "application/octet-stream",
    )


def _find_import(import_id):
    return next((item for item in portal_state["imports"] if item["id"] == import_id), None)


@certificate_api.get("/imports/<import_id>")
def import_status(import_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = _find_import(import_id)
    if not job:
        return _error("NOT_FOUND", "Import job not found.", 404)
    return _response({key: value for key, value in job.items() if key not in ("rawRows", "mapping")})


@certificate_api.get("/imports/<import_id>/preview")
def import_preview(import_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = _find_import(import_id)
    if not job:
        return _error("NOT_FOUND", "Import job not found.", 404)
    return _response({"columns": job["columns"], "records": job["rawRows"]})


@certificate_api.post("/imports/<import_id>/mapping")
def import_mapping(import_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = _find_import(import_id)
    if not job:
        return _error("NOT_FOUND", "Import job not found.", 404)
    job["mapping"] = request.get_json(silent=True).get("mapping", {}) if request.is_json else {}
    job["status"] = "PROCESSING"
    _save_state()
    return _response({"importId": import_id, "status": job["status"], "mapping": job["mapping"]})


def _validate_job_records(job, mapping=None):
    rows = job.get("rawRows") or []
    if not rows:
        return False
    mapping = mapping or job.get("mapping") or {}
    records = [_participant_from_row(row, mapping, index) for index, row in enumerate(rows)]
    seen = set()
    duplicates = 0
    for record in records:
        email = record.get("email", "").strip().lower()
        roll = record.get("rollNumber", "").strip().lower()
        identity = (email, roll)
        # Missing identifiers are validation errors, not duplicate records.
        if (email or roll) and identity in seen:
            duplicates += 1
            record["validationErrors"].append("DUPLICATE_RECORD")
        if email or roll:
            seen.add(identity)
    job["records"] = records
    job["status"] = "VALIDATED"
    job["validRecords"] = sum(not r["validationErrors"] for r in records)
    job["invalidRecords"] = len(records) - job["validRecords"]
    job["duplicateRecords"] = duplicates
    job["missingNames"] = sum("MISSING_NAME" in r["validationErrors"] for r in records)
    job["missingEmails"] = sum("INVALID_EMAIL" in r["validationErrors"] for r in records)
    job["missingIds"] = sum("MISSING_STUDENT_ID" in r["validationErrors"] for r in records)
    job["missingRollNumbers"] = sum("MISSING_ROLL_NUMBER" in r["validationErrors"] for r in records)
    job["missingCheckIn"] = sum("MISSING_CHECK_IN" in r["validationErrors"] for r in records)
    job["missingCheckOut"] = sum("MISSING_CHECK_OUT" in r["validationErrors"] for r in records)
    return True


@certificate_api.post("/imports/<import_id>/validate")
def validate_import(import_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = _find_import(import_id)
    if not job:
        return _error("NOT_FOUND", "Import job not found.", 404)
    body = request.get_json(silent=True) or {}
    if body.get("mapping"):
        job["mapping"] = body["mapping"]
    if not _validate_job_records(job, job.get("mapping")):
        return _error("INVALID_STATE", "Import job has no rows to validate.", 422)
    _save_state()
    _audit("FILE_PROCESSED", job["filename"])
    records = job["records"]
    errors = [{"row": r["sourceRow"], "codes": r["validationErrors"]} for r in records if r["validationErrors"]]
    return _response({"status": "VALIDATED", "totalRecords": len(records), "validRecords": job["validRecords"],
                      "invalidRecords": job["invalidRecords"], "duplicateRecords": job["duplicateRecords"],
                      "eligibleRecords": sum(r["eligibility"] == "ELIGIBLE" for r in records),
                      "ineligibleRecords": sum(r["eligibility"] == "NOT_ELIGIBLE" for r in records),
                      "pendingRecords": sum(r["eligibility"] == "PENDING" for r in records), "errors": errors})


@certificate_api.post("/imports/<import_id>/confirm")
def confirm_import(import_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = _find_import(import_id)
    if not job:
        return _error("NOT_FOUND", "Import job not found.", 404)
    body = request.get_json(silent=True) or {}
    if job.get("status") != "VALIDATED":
        if not _validate_job_records(job, body.get("mapping") or job.get("mapping")):
            return _error("INVALID_STATE", "Import must be validated before confirmation.", 422)
    if job.get("status") == "IMPORTED":
        return _response({"importId": import_id, "status": "IMPORTED", "participantsCreated": 0,
                          "certificateRequestsCreated": 0})
    # Persist every real row from the uploaded file. Validation/eligibility only
    # controls certificate creation; it must never silently discard imported data.
    # The legacy flag is accepted for backwards compatibility but is intentionally
    # ignored for participant persistence.
    records = list(job.get("records") or [])

    # Avoid creating duplicate participant rows when the same import is confirmed
    # again or an identical file is uploaded twice. Match on stable real-world
    # identifiers rather than the generated participant UUID.
    existing_keys = set()
    for participant in portal_state["participants"]:
        for key in ("email", "rollNumber", "studentId"):
            value = str(participant.get(key) or "").strip().lower()
            if value:
                existing_keys.add((key, value))

    new_records = []
    for record in records:
        identity_keys = []
        for key in ("email", "rollNumber", "studentId"):
            value = str(record.get(key) or "").strip().lower()
            if value:
                identity_keys.append((key, value))
        if identity_keys and any(key in existing_keys for key in identity_keys):
            continue
        new_records.append(record)
        existing_keys.update(identity_keys)

    records = new_records
    portal_state["participants"].extend(records)
    # Eligibility is a manual admin decision. Importing attendance data never
    # creates a certificate request automatically. A certificate request is
    # created only when an administrator explicitly marks a participant eligible.
    created = []
    job["status"] = "IMPORTED"
    job["participantsPersisted"] = len(records)
    job["certificateRequestsCreated"] = len(created)
    _save_state()
    return _response({"importId": import_id, "status": "IMPORTED", "participantsCreated": len(records),
                      "certificateRequestsCreated": len(created),
                      "totalImportedRows": len(job.get("records") or []),
                      "duplicatesSkipped": max(0, len(job.get("records") or []) - len(records))})


@certificate_api.get("/participants")
def participants():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    items = list(portal_state["participants"])
    search = request.args.get("search", "").strip().lower()
    if search:
        items = [p for p in items if search in json.dumps(p).lower()]
    if request.args.get("eligibility") and request.args["eligibility"] != "ALL":
        items = [p for p in items if p["eligibility"] == request.args["eligibility"]]
    if request.args.get("certificateStatus") and request.args["certificateStatus"] != "ALL":
        items = [p for p in items if p.get("certificateStatus") == request.args["certificateStatus"]]
    paginated = _paginate(items)
    paginated["total"] = paginated["pagination"]["total"]
    paginated["totalPages"] = paginated["pagination"]["totalPages"]
    return _response(paginated)


@certificate_api.get("/participants/<participant_id>")
def participant_detail(participant_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = next((p for p in portal_state["participants"] if p["id"] == participant_id), None)
    return _response(item) if item else _error("NOT_FOUND", "Participant not found.", 404)


@certificate_api.post("/participants/<participant_id>/eligibility")
def update_participant_eligibility(participant_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)

    participant = next((p for p in portal_state["participants"] if p.get("id") == participant_id), None)
    if not participant:
        return _error("NOT_FOUND", "Participant not found.", 404)

    body = request.get_json(silent=True) or {}
    decision = str(body.get("eligibility") or "").strip().upper()
    if decision not in {"ELIGIBLE", "NOT_ELIGIBLE"}:
        return _error("INVALID_DECISION", "Eligibility must be ELIGIBLE or NOT_ELIGIBLE.", 422)

    existing = next((c for c in portal_state["certificates"] if c.get("participantId") == participant_id), None)
    if existing and existing.get("status") not in ("PENDING", "REJECTED"):
        return _error("INVALID_STATE", "Eligibility cannot be changed after certificate processing has started.", 422)

    participant["eligibility"] = decision
    participant["eligibilityReason"] = "Approved by administrator" if decision == "ELIGIBLE" else "Rejected by administrator"

    if decision == "NOT_ELIGIBLE":
        participant["certificateStatus"] = "REJECTED"
        if existing and existing.get("status") == "PENDING":
            existing["status"] = "REJECTED"
            existing["rejectionReason"] = "Participant marked not eligible by administrator."
            existing["rejectedBy"] = os.getenv("ADMIN_EMAIL", "administrator")
            existing["rejectedAt"] = _now()
    else:
        participant["certificateStatus"] = "PENDING"
        if not existing:
            prefix = portal_state["settings"].get("certificateIdPrefix") or "CERT"
            number = len(portal_state["certificates"]) + 1
            certificate_id = f"{prefix}-{number:05d}"
            template_id = portal_state["settings"].get("activeTemplateId") or ""
            template = next((t for t in portal_state["templates"] if t.get("id") == template_id), None)
            portal_state["certificates"].append({
                "id": f"cert_{uuid.uuid4().hex[:12]}",
                "certificateId": certificate_id,
                "participantId": participant["id"],
                "participantName": participant["name"],
                "participantEmail": participant["email"],
                "participantRollNumber": participant["rollNumber"],
                "participantStudentId": participant["studentId"],
                "checkIn": participant["checkIn"],
                "checkOut": participant["checkOut"],
                "templateId": template_id,
                "templateName": template.get("name", "") if template else "",
                "status": "PENDING",
                "eventName": portal_state["settings"].get("eventName", ""),
                "issueDate": portal_state["settings"].get("issueDate") or _now()[:10],
            })
        elif existing.get("status") == "REJECTED":
            existing.update({"status": "PENDING", "rejectionReason": "", "rejectedBy": "", "rejectedAt": ""})

    _save_state()
    _audit("ELIGIBILITY_DECISION", participant.get("name", participant_id), details=f"Admin decision: {decision}")
    return _response(participant, message=f"Participant marked {decision.replace('_', ' ').lower()}.")


@certificate_api.post("/participants/bulk-eligibility")
def bulk_participant_eligibility():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)

    body = request.get_json(silent=True) or {}
    decision = str(body.get("eligibility") or "").strip().upper()
    if decision not in {"ELIGIBLE", "NOT_ELIGIBLE"}:
        return _error("INVALID_DECISION", "Eligibility must be ELIGIBLE or NOT_ELIGIBLE.", 422)

    apply_all = bool(body.get("applyAll"))
    ids = {str(item) for item in (body.get("ids") or []) if item}
    if not apply_all and not ids:
        return _error("NO_SELECTION", "Select participants or choose Apply to All.", 422)

    targets = [p for p in portal_state["participants"] if apply_all or str(p.get("id")) in ids]
    changed = 0
    certificates_created = 0
    skipped = 0

    for participant in targets:
        participant_id = str(participant.get("id"))
        existing = next((c for c in portal_state["certificates"] if c.get("participantId") == participant_id), None)

        # Do not overwrite participants whose certificate processing has already started.
        if existing and existing.get("status") not in ("PENDING", "REJECTED"):
            skipped += 1
            continue

        participant["eligibility"] = decision
        participant["eligibilityReason"] = (
            "Approved by administrator" if decision == "ELIGIBLE" else "Rejected by administrator"
        )
        changed += 1

        if decision == "NOT_ELIGIBLE":
            participant["certificateStatus"] = "REJECTED"
            if existing and existing.get("status") == "PENDING":
                existing["status"] = "REJECTED"
                existing["rejectionReason"] = "Participant marked not eligible by administrator."
                existing["rejectedBy"] = os.getenv("ADMIN_EMAIL", "administrator")
                existing["rejectedAt"] = _now()
            continue

        participant["certificateStatus"] = "PENDING"
        if not existing:
            prefix = portal_state["settings"].get("certificateIdPrefix") or "CERT"
            number = len(portal_state["certificates"]) + 1
            certificate_id = f"{prefix}-{number:05d}"
            template_id = portal_state["settings"].get("activeTemplateId") or ""
            template = next((t for t in portal_state["templates"] if t.get("id") == template_id), None)
            portal_state["certificates"].append({
                "id": f"cert_{uuid.uuid4().hex[:12]}",
                "certificateId": certificate_id,
                "participantId": participant["id"],
                "participantName": participant["name"],
                "participantEmail": participant["email"],
                "participantRollNumber": participant["rollNumber"],
                "participantStudentId": participant["studentId"],
                "checkIn": participant["checkIn"],
                "checkOut": participant["checkOut"],
                "templateId": template_id,
                "templateName": template.get("name", "") if template else "",
                "status": "PENDING",
                "eventName": portal_state["settings"].get("eventName", ""),
                "issueDate": portal_state["settings"].get("issueDate") or _now()[:10],
            })
            certificates_created += 1
        elif existing.get("status") == "REJECTED":
            existing.update({"status": "PENDING", "rejectionReason": "", "rejectedBy": "", "rejectedAt": ""})

    _save_state()
    _audit(
        "BULK_ELIGIBILITY_DECISION",
        f"{changed} participant(s)",
        details=f"Bulk admin decision: {decision}; applyAll={apply_all}; skipped={skipped}",
    )
    return _response({
        "updated": True,
        "participantsUpdated": changed,
        "certificatesCreated": certificates_created,
        "skipped": skipped,
        "applyAll": apply_all,
    }, message=f"{changed} participant(s) marked {decision.replace('_', ' ').lower()}.")


@certificate_api.delete("/participants/<participant_id>")
def delete_participant(participant_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)

    participant = next((p for p in portal_state["participants"] if p.get("id") == participant_id), None)
    if not participant:
        return _error("NOT_FOUND", "Participant not found.", 404)

    before_participants = len(portal_state["participants"])
    portal_state["participants"] = [p for p in portal_state["participants"] if p.get("id") != participant_id]

    removed_certificates = [c for c in portal_state["certificates"] if c.get("participantId") == participant_id]
    removed_certificate_ids = {c.get("certificateId") for c in removed_certificates}
    portal_state["certificates"] = [c for c in portal_state["certificates"] if c.get("participantId") != participant_id]

    portal_state["emailJobs"] = [
        j for j in portal_state["emailJobs"]
        if j.get("certificateId") not in removed_certificate_ids
    ]
    for certificate_id in removed_certificate_ids:
        db.delete_file(f"certificate:{certificate_id}")

    _save_state()
    _audit("PARTICIPANT_DELETED", participant.get("name", participant_id), details=f"Deleted participant {participant_id} and {len(removed_certificates)} certificate record(s).")
    return _response({
        "deleted": True,
        "participantId": participant_id,
        "certificatesDeleted": len(removed_certificates),
        "participantsRemaining": before_participants - 1,
    }, message="Participant and related certificate records deleted.")


@certificate_api.post("/participants/bulk-delete")
def bulk_delete_participants():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)

    body = request.get_json(silent=True) or {}
    delete_all = bool(body.get("deleteAll"))
    ids = {str(item) for item in (body.get("ids") or []) if item}

    if not delete_all and not ids:
        return _error("NO_SELECTION", "Select at least one participant or choose Delete All.", 422)

    if delete_all:
        target_ids = {str(p.get("id")) for p in portal_state["participants"]}
    else:
        target_ids = ids

    before = len(portal_state["participants"])
    removed_participants = [p for p in portal_state["participants"] if str(p.get("id")) in target_ids]
    removed_participant_ids = {str(p.get("id")) for p in removed_participants}
    portal_state["participants"] = [p for p in portal_state["participants"] if str(p.get("id")) not in removed_participant_ids]

    removed_certificates = [c for c in portal_state["certificates"] if str(c.get("participantId")) in removed_participant_ids]
    removed_certificate_ids = {str(c.get("certificateId")) for c in removed_certificates}
    portal_state["certificates"] = [c for c in portal_state["certificates"] if str(c.get("participantId")) not in removed_participant_ids]

    before_jobs = len(portal_state["emailJobs"])
    portal_state["emailJobs"] = [j for j in portal_state["emailJobs"] if str(j.get("certificateId")) not in removed_certificate_ids]
    removed_email_jobs = before_jobs - len(portal_state["emailJobs"])
    for certificate_id in removed_certificate_ids:
        db.delete_file(f"certificate:{certificate_id}")

    _save_state()
    action = "PARTICIPANTS_BULK_DELETED" if delete_all else "PARTICIPANTS_SELECTED_DELETED"
    _audit(action, f"{len(removed_participants)} participant(s)", details=f"Deleted {len(removed_participants)} participant(s), {len(removed_certificates)} certificate(s), and {removed_email_jobs} email log(s).")
    return _response({
        "deleted": True,
        "participantsDeleted": len(removed_participants),
        "certificatesDeleted": len(removed_certificates),
        "emailLogsDeleted": removed_email_jobs,
        "participantsRemaining": before - len(removed_participants),
    }, message="All matching participant records and related certificate/email records deleted." if delete_all else "Selected participants and related records deleted.")


@certificate_api.get("/templates")
def templates():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)

    # Only expose templates whose uploaded bytes actually exist in persistent
    # storage. This prevents stale database metadata from appearing as a real
    # certificate template after a storage migration or accidental file loss.
    available = []
    stale_ids = []
    dimensions_changed = False
    for template in portal_state["templates"]:
        stored = db.load_file(f"template:{template['id']}")
        if stored is None:
            stale_ids.append(template["id"])
            continue
        if not template.get("pageWidth") or not template.get("pageHeight"):
            try:
                extension = str(template.get("fileType") or Path(template.get("name") or "").suffix).lower().lstrip(".")
                if extension == "pdf":
                    reader = PdfReader(io.BytesIO(stored["data"]))
                    if reader.pages:
                        template["pageWidth"] = float(reader.pages[0].mediabox.width)
                        template["pageHeight"] = float(reader.pages[0].mediabox.height)
                else:
                    size = ImageReader(io.BytesIO(stored["data"])).getSize()
                    if size[0] and size[1]:
                        template["pageWidth"] = 842.0
                        template["pageHeight"] = 842.0 * float(size[1]) / float(size[0])
                dimensions_changed = True
            except Exception:
                pass
        available.append(template)

    if stale_ids or dimensions_changed:
        portal_state["templates"] = available
        active_id = portal_state["settings"].get("activeTemplateId")
        if active_id not in {item["id"] for item in available}:
            new_active = available[0] if available else None
            for item in available:
                item["active"] = item["id"] == (new_active["id"] if new_active else "")
            portal_state["settings"]["activeTemplateId"] = new_active["id"] if new_active else ""
        _save_state()

    return _response(available)


@certificate_api.get("/templates/<template_id>")
def template_detail(template_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    template = next((item for item in portal_state["templates"] if item["id"] == template_id), None)
    if not template:
        return _error("NOT_FOUND", "Template not found.", 404)
    return _response(template)


@certificate_api.post("/templates")
def create_template():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    uploaded = request.files.get("file")
    if not uploaded:
        return _error("FILE_REQUIRED", "Certificate template file is required.", 422)
    extension = Path(uploaded.filename or "").suffix.lower().lstrip(".")
    if extension not in ("pdf", "png", "jpg", "jpeg"):
        return _error("INVALID_FILE", "Only PDF, PNG, JPG, and JPEG templates are supported.", 422)
    raw_template = uploaded.read()
    if not raw_template:
        return _error("EMPTY_FILE", "The certificate template file is empty.", 422)
    if len(raw_template) > 25 * 1024 * 1024:
        return _error("FILE_TOO_LARGE", "Certificate templates are limited to 25 MB.", 413)
    template_id = f"tpl_{uuid.uuid4().hex[:12]}"
    filename = f"{template_id}.{extension}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    page_width, page_height = 842.0, 595.0
    try:
        if extension == "pdf":
            reader = PdfReader(io.BytesIO(raw_template))
            if reader.pages:
                page_width = float(reader.pages[0].mediabox.width)
                page_height = float(reader.pages[0].mediabox.height)
        else:
            image_reader = ImageReader(io.BytesIO(raw_template))
            iw, ih = image_reader.getSize()
            if iw and ih:
                page_width = 842.0
                page_height = page_width * float(ih) / float(iw)
    except Exception:
        pass
    db.save_file(f"template:{template_id}", filename, raw_template, content_type)
    item = {"id": template_id, "name": request.form.get("name") or uploaded.filename,
            "fileType": extension.upper(), "filePath": filename, "previewUrl": f"/api/v1/templates/{template_id}/file",
            "pageWidth": page_width, "pageHeight": page_height,
            "active": not portal_state["templates"], "uploadedAt": _now(), "usageCount": 0, "fields": [], "version": 1, "versions": []}
    portal_state["templates"].append(item)
    if item["active"]:
        portal_state["settings"]["activeTemplateId"] = template_id
    _save_state()
    _audit("TEMPLATE_UPLOADED", item["name"])
    return _response({key: value for key, value in item.items() if key != "filePath"}, status=201)


@certificate_api.get("/templates/<template_id>/file")
def template_file(template_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = next((t for t in portal_state["templates"] if t["id"] == template_id), None)
    if not item:
        return _error("NOT_FOUND", "Template not found.", 404)
    stored = db.load_file(f"template:{template_id}")
    if not stored:
        return _error("NOT_FOUND", "Template file not found.", 404)
    return send_file(io.BytesIO(stored["data"]), download_name=item["name"],
                     mimetype=stored.get("content_type") or "application/octet-stream")


@certificate_api.post("/templates/<template_id>/activate")
def activate_template(template_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = next((t for t in portal_state["templates"] if t["id"] == template_id), None)
    if not item:
        return _error("NOT_FOUND", "Template not found.", 404)
    for template in portal_state["templates"]:
        template["active"] = template["id"] == template_id
    portal_state["settings"]["activeTemplateId"] = template_id
    _save_state()
    return _response({"templateId": template_id, "active": True})


@certificate_api.put("/templates/<template_id>/fields")
def update_template_fields(template_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = next((t for t in portal_state["templates"] if t["id"] == template_id), None)
    if not item:
        return _error("NOT_FOUND", "Template not found.", 404)
    fields = (request.get_json(silent=True) or {}).get("fields")
    if not isinstance(fields, list):
        return _error("INVALID_FIELDS", "Fields must be an array.", 422)
    previous = item.get("fields") or []
    versions = item.setdefault("versions", [])
    next_version = int(item.get("version") or 1)
    if previous != fields:
        versions.append({"version": next_version, "savedAt": _now(), "fields": previous})
        versions[:] = versions[-10:]
        next_version += 1
    item["fields"] = fields
    item["version"] = next_version
    _save_state()
    _audit("TEMPLATE_UPDATED", item["name"], details=f"Template version {next_version} saved.")
    return _response(item, message=f"Template saved as version {next_version}.")


@certificate_api.get("/templates/<template_id>/versions")
def template_versions(template_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = next((t for t in portal_state["templates"] if t["id"] == template_id), None)
    if not item:
        return _error("NOT_FOUND", "Template not found.", 404)
    current = {"version": int(item.get("version") or 1), "savedAt": item.get("updatedAt") or item.get("uploadedAt"), "current": True, "fields": item.get("fields") or []}
    history = list(reversed(item.get("versions") or []))
    return _response([current] + [{**v, "current": False} for v in history])


@certificate_api.post("/templates/<template_id>/versions/<int:version>/restore")
def restore_template_version(template_id, version):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = next((t for t in portal_state["templates"] if t["id"] == template_id), None)
    if not item:
        return _error("NOT_FOUND", "Template not found.", 404)
    snapshot = next((v for v in item.get("versions") or [] if int(v.get("version", -1)) == version), None)
    if not snapshot:
        return _error("VERSION_NOT_FOUND", f"Template version {version} was not found.", 404)
    current = item.get("fields") or []
    versions = item.setdefault("versions", [])
    current_version = int(item.get("version") or 1)
    versions.append({"version": current_version, "savedAt": _now(), "fields": current})
    versions[:] = versions[-10:]
    item["fields"] = snapshot.get("fields") or []
    item["version"] = current_version + 1
    _save_state()
    _audit("TEMPLATE_VERSION_RESTORED", item["name"], details=f"Restored version {version} as new version {item['version']}.")
    return _response(item, message=f"Version {version} restored as version {item['version']}.")


@certificate_api.post("/templates/<template_id>/preview")
def template_preview(template_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = next((t for t in portal_state["templates"] if t["id"] == template_id), None)
    if not item:
        return _error("NOT_FOUND", "Template not found.", 404)
    body = request.get_json(silent=True) or {}
    participant = body.get("participant") or {}
    sample = {
        "certificateId": str(participant.get("certificateId") or "PREVIEW-001"),
        "participantName": str(participant.get("name") or "Preview Participant"),
        "participantEmail": str(participant.get("email") or "preview@example.com"),
        "participantStudentId": str(participant.get("studentId") or "STUDENT-001"),
        "participantRollNumber": str(participant.get("rollNumber") or "ROLL-001"),
        "eventName": str(participant.get("eventName") or portal_state.get("settings", {}).get("eventName") or "Certificate Event"),
        "issueDate": str(participant.get("date") or portal_state.get("settings", {}).get("issueDate") or datetime.now(timezone.utc).date().isoformat()),
    }
    try:
        output = _render_certificate(sample, item, store=False)
        return send_file(io.BytesIO(output), download_name=f"{item['name']}-preview.pdf", mimetype="application/pdf")
    except (OSError, RuntimeError, ValueError) as exc:
        return _error("PREVIEW_FAILED", str(exc), 422)


@certificate_api.delete("/templates/<template_id>")
def delete_template(template_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = next((t for t in portal_state["templates"] if t["id"] == template_id), None)
    if not item:
        return _error("NOT_FOUND", "Template not found.", 404)
    was_active = bool(item.get("active"))
    portal_state["templates"].remove(item)
    if was_active and portal_state["templates"]:
        portal_state["templates"][0]["active"] = True
        portal_state["settings"]["activeTemplateId"] = portal_state["templates"][0]["id"]
    elif was_active:
        portal_state["settings"]["activeTemplateId"] = ""
    _save_state()
    db.delete_file(f"template:{template_id}")
    return _response(True)


def _certificate_filtered():
    items = list(portal_state["certificates"])
    status = request.args.get("status")
    search = request.args.get("search", "").lower()
    if status and status != "ALL":
        items = [c for c in items if c["status"] == status]
    if search:
        items = [c for c in items if search in json.dumps(c).lower()]
    return items


@certificate_api.get("/certificates")
def certificates():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    items = _certificate_filtered()
    result = _paginate(items)
    result["countsByStatus"] = {status: sum(c["status"] == status for c in portal_state["certificates"])
                                for status in ("PENDING", "APPROVED", "REJECTED", "GENERATING", "GENERATED", "EMAIL_QUEUED", "SENT", "FAILED")}
    result["total"] = result["pagination"]["total"]
    return _response(result)


@certificate_api.get("/certificates/<certificate_id>")
def certificate_detail(certificate_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = _find_certificate(certificate_id)
    if item:
        return _response(item)
    participant = next((p for p in portal_state["participants"]
                       if p.get("rollNumber") == certificate_id or p.get("studentId") == certificate_id or p.get("email") == certificate_id), None)
    if participant:
        related = next((c for c in portal_state["certificates"] if c.get("participantId") == participant.get("id")), None)
        return _response({"participant": participant, "status": related["status"] if related else "PENDING",
                          "certificateId": related.get("certificateId") if related else None})
    return _error("NOT_FOUND", "Certificate not found.", 404)


def _find_certificate(certificate_id):
    return next((c for c in portal_state["certificates"] if c["id"] == certificate_id or c["certificateId"] == certificate_id), None)


def _find_certificate_for_participant(identifier):
    participant = next((p for p in portal_state["participants"]
                       if str(p.get("rollNumber", "")).lower() == str(identifier).lower()
                       or str(p.get("studentId", "")).lower() == str(identifier).lower()
                       or str(p.get("email", "")).lower() == str(identifier).lower()), None)
    if not participant:
        return None
    return next((c for c in portal_state["certificates"] if c.get("participantId") == participant.get("id")), None)


def _find_participant_for_identifier(identifier):
    identifier = str(identifier).strip()
    return next((p for p in portal_state["participants"]
                 if str(p.get("rollNumber", "")).lower() == identifier.lower()
                 or str(p.get("studentId", "")).lower() == identifier.lower()
                 or str(p.get("email", "")).lower() == identifier.lower()), None)


@certificate_api.get("/certificates/lookup/<identifier>")
def lookup_certificate_by_identifier(identifier):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    participant = _find_participant_for_identifier(identifier)
    if not participant:
        return _error("NOT_FOUND", "Participant not found.", 404)
    certificate = next((c for c in portal_state["certificates"] if c.get("participantId") == participant.get("id")), None)
    return _response({"participant": participant, "certificate": certificate, "status": certificate["status"] if certificate else "PENDING"})


def _approve_generate_send(item, comment=""):
    """Approve a pending certificate, generate its PDF, and email it.

    This is the single production workflow used by the admin approval action.
    Every stage persists its state so failures are visible and retryable.
    """
    if item.get("status") != "PENDING":
        raise ValueError("Only pending certificates can be approved.")

    participant_name = str(item.get("participantName") or "").strip()
    participant_email = str(item.get("participantEmail") or "").strip()
    if not participant_name:
        raise ValueError("Participant name is empty; approval was stopped.")
    if not participant_email or "@" not in participant_email:
        raise ValueError("Participant email is missing or invalid; approval was stopped.")

    item.update({
        "status": "APPROVED",
        "approvedBy": os.getenv("ADMIN_EMAIL", "administrator"),
        "approvedAt": _now(),
        "approvalComment": comment or "",
        "failureReason": "",
    })
    _save_state()
    _audit("CERTIFICATE_APPROVED", item["certificateId"])

    template_id = item.get("templateId") or portal_state.get("settings", {}).get("activeTemplateId") or ""
    template = next((t for t in portal_state["templates"] if t.get("id") == template_id), None)
    if not template:
        item.update({"status": "FAILED", "failureReason": "Certificate template not found. Upload and activate a certificate template first."})
        _save_state()
        raise RuntimeError(item["failureReason"])

    try:
        file_key = _render_certificate(item, template)
        item.update({
            "status": "GENERATED",
            "generatedAt": _now(),
            "certificateUrl": f"/api/v1/certificates/{item['certificateId']}/download",
            "templateId": template.get("id", template_id),
            "templateName": template.get("name", ""),
            "failureReason": "",
        })
        _save_state()
        _audit("CERTIFICATE_GENERATED", item["certificateId"])

        stored_certificate = db.load_file(file_key)
        if not stored_certificate:
            raise RuntimeError("Generated certificate file is not available to attach.")

        email_job_id = f"email_{uuid.uuid4().hex[:12]}"
        subject, body = _render_email_content(item)
        job = {
            "id": email_job_id,
            "jobId": email_job_id,
            "certificateId": item["certificateId"],
            "recipient": participant_email,
            "studentName": participant_name,
            "status": "PROCESSING",
            "createdAt": _now(),
            "sentAt": None,
            "attempts": 1,
            "subject": subject,
            "body": body,
            "attachmentFilename": stored_certificate.get("filename") or f"{item['certificateId']}.pdf",
        }
        portal_state["emailJobs"].append(job)
        _save_state()

        _deliver_certificate_email(item, stored_certificate, subject, body)
        sent_at = _now()
        job.update({"status": "SENT", "sentAt": sent_at, "error": ""})
        item.update({"status": "SENT", "sentAt": sent_at, "emailDeliveryStatus": "SENT", "failureReason": ""})
        _audit("EMAIL_SENT", item["certificateId"])
        _save_state()
        return item, job
    except Exception as exc:
        item.update({"status": "FAILED", "emailDeliveryStatus": "FAILED", "failureReason": str(exc)})
        if "job" in locals():
            job.update({"status": "FAILED", "error": str(exc)})
        _audit("CERTIFICATE_WORKFLOW_FAILED", item["certificateId"], "FAILED", str(exc))
        _save_state()
        raise


@certificate_api.post("/certificates/lookup/<identifier>/approve")
def approve_certificate_by_identifier(identifier):
    """Approve, generate, and email a certificate in one operation."""
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    participant = _find_participant_for_identifier(identifier)
    if not participant:
        return _error("NOT_FOUND", "Participant not found.", 404)
    item = next((c for c in portal_state["certificates"] if c.get("participantId") == participant.get("id")), None)
    if not item:
        return _error("NOT_FOUND", "Certificate not found. Mark the participant eligible first.", 404)
    try:
        item, job = _approve_generate_send(item, (request.get_json(silent=True) or {}).get("comment", ""))
        return _response({
            "participant": participant,
            "certificate": item,
            "sentToEmail": item["participantEmail"],
            "emailJobId": job["jobId"],
        }, message="Certificate approved, generated, and emailed successfully.")
    except ValueError as exc:
        return _error("INVALID_STATE", str(exc), 422)
    except (RuntimeError, OSError, smtplib.SMTPException) as exc:
        return _error("CERTIFICATE_WORKFLOW_FAILED", str(exc), 502)


@certificate_api.post("/certificates/<certificate_id>/approve")
def approve_certificate(certificate_id):
    """One-click admin workflow: approve -> generate PDF -> email."""
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = _find_certificate(certificate_id) or _find_certificate_for_participant(certificate_id)
    if not item:
        return _error("NOT_FOUND", "Certificate not found.", 404)
    try:
        item, job = _approve_generate_send(item, (request.get_json(silent=True) or {}).get("comment", ""))
        return _response({
            "certificate": item,
            "emailJobId": job["jobId"],
            "sentToEmail": item.get("participantEmail", ""),
        }, message="Certificate approved, generated, and emailed successfully.")
    except ValueError as exc:
        return _error("INVALID_STATE", str(exc), 422)
    except (RuntimeError, OSError, smtplib.SMTPException) as exc:
        return _error("CERTIFICATE_WORKFLOW_FAILED", str(exc), 502)


@certificate_api.post("/certificates/<certificate_id>/reject")
def reject_certificate(certificate_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = _find_certificate(certificate_id)
    if not item:
        return _error("NOT_FOUND", "Certificate not found.", 404)
    item.update({"status": "REJECTED", "rejectionReason": (request.get_json(silent=True) or {}).get("reason", ""),
                 "rejectedBy": os.getenv("ADMIN_EMAIL", "administrator"), "rejectedAt": _now()})
    _save_state()
    _audit("CERTIFICATE_REJECTED", item["certificateId"], "WARNING", item.get("rejectionReason", ""))
    return _response(item)


@certificate_api.post("/certificates/bulk-approve")
def bulk_approve():
    """Run the same complete workflow for every selected pending certificate."""
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    ids = (request.get_json(silent=True) or {}).get("certificateIds", [])
    results, approved = [], 0
    for identifier in ids:
        item = _find_certificate(identifier) or _find_certificate_for_participant(identifier)
        if not item:
            results.append({"id": identifier, "status": "FAILED", "reason": "Certificate not found."})
            continue
        try:
            item, job = _approve_generate_send(item)
            approved += 1
            results.append({"id": identifier, "status": item.get("status", "SENT"), "emailJobId": job["jobId"], "sentToEmail": item.get("participantEmail", "")})
        except (ValueError, RuntimeError, OSError, smtplib.SMTPException) as exc:
            results.append({"id": identifier, "status": "FAILED", "reason": str(exc)})

    failed = len(ids) - approved
    return _response({
        "total": len(ids),
        "approved": approved,
        "approvedCount": approved,
        "failed": failed,
        "sentCount": approved,
        "results": results,
    }, message=f"Completed {approved} of {len(ids)} certificate workflows.")


@certificate_api.post("/certificates/bulk-generate")
def bulk_generate():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    ids = (request.get_json(silent=True) or {}).get("certificateIds", [])
    completed = 0
    results = []
    for identifier in ids:
        item = _find_certificate(identifier) or _find_certificate_for_participant(identifier)
        if not item:
            results.append({"id": identifier, "status": "FAILED", "reason": "Certificate not found."})
            continue
        try:
            if item.get("status") not in ("APPROVED", "FAILED"):
                raise ValueError("Certificate must be approved before generation.")
            template_id = item.get("templateId") or portal_state.get("settings", {}).get("activeTemplateId") or ""
            template = next((t for t in portal_state["templates"] if t.get("id") == template_id), None)
            if not template:
                raise RuntimeError("Certificate template not found.")
            file_key = _render_certificate(item, template)
            item.update({"status": "GENERATED", "generatedAt": _now(), "certificateUrl": f"/api/v1/certificates/{item['certificateId']}/download", "failureReason": ""})
            completed += 1
            results.append({"id": identifier, "status": "GENERATED", "fileKey": file_key})
        except (ValueError, RuntimeError, OSError) as exc:
            item.update({"status": "FAILED", "failureReason": str(exc)})
            results.append({"id": identifier, "status": "FAILED", "reason": str(exc)})
    _save_state()
    return _response({"count": completed, "results": results, "filename": "certificates.zip"}, message=f"Generated {completed} certificate(s).")


def _field_font_name(field):
    family = str(field.get("fontFamily") or "Inter")
    style = str(field.get("fontStyle") or "normal").strip().lower()
    weight = str(field.get("fontWeight") or "normal").strip().lower()
    bold = weight in {"bold", "semibold"} or style in {"bold", "bold italic"}
    italic = style in {"italic", "bold italic"}
    families = {
        "Cinzel": "CertCinzel",
        "Playfair Display": "CertPlayfair",
        "Inter": "CertInter",
        "Plus Jakarta Sans": "CertJakarta",
        "Great Vibes": "CertVibes",
    }
    base = families.get(family, "CertInter")
    suffix = "-BoldItalic" if bold and italic else "-Bold" if bold else "-Italic" if italic else ""
    candidate = base + suffix
    if candidate in pdfmetrics.getRegisteredFontNames():
        return candidate
    return base if base in pdfmetrics.getRegisteredFontNames() else "Helvetica"


def _draw_field(overlay, field, value, width, height):
    if field.get("visible", True) is False or value == "":
        return
    raw_key = field.get("key") or field.get("fieldKey") or ""
    key = str(raw_key).strip().upper().replace("{{", "").replace("}}", "")
    aliases = {"FULL_NAME": "NAME", "PARTICIPANT_NAME": "NAME", "STUDENTNAME": "NAME", "ROLLNUMBER": "ROLL_NO", "STUDENTID": "STUDENT_ID", "EVENT": "EVENT_NAME", "CERTIFICATEID": "CERTIFICATE_ID"}
    key = aliases.get(key, key)
    x = max(0.0, min(100.0, float(field.get("xPercent", field.get("x", 50))))) / 100.0 * width
    center_y = height - (max(0.0, min(100.0, float(field.get("yPercent", field.get("y", 50))))) / 100.0 * height)
    overlay.setFillColor(field.get("color", "#111827"))
    font_name = _field_font_name(field)
    font_size = max(1.0, float(field.get("fontSize", 24)))
    overlay.setFont(font_name, font_size)
    # The editor defines Y as the vertical center of the text box. ReportLab's
    # drawString/drawCentredString use a baseline, so convert the center anchor
    # to a baseline using the selected font's real ascent/descent metrics.
    try:
        ascent, descent = pdfmetrics.getAscentDescent(font_name, font_size)
        y = center_y - ((ascent + descent) / 2.0)
    except Exception:
        y = center_y - font_size * 0.35
    alignment = field.get("textAlign", "center")
    if alignment == "left":
        overlay.drawString(x, y, value)
    elif alignment == "right":
        overlay.drawRightString(x, y, value)
    else:
        overlay.drawCentredString(x, y, value)


def _render_certificate(item, template, store=True):
    if PdfReader is None or canvas is None:
        raise RuntimeError("Certificate rendering dependencies are not installed.")
    participant_name = str(item.get("participantName") or item.get("participant", {}).get("name") or "").strip()
    if not participant_name:
        raise RuntimeError("Participant name is empty; certificate generation was stopped.")
    stored_template = db.load_file(f"template:{template['id']}")
    if not stored_template:
        raise RuntimeError("Template file not found in persistent storage.")
    fields = template.get("fields") or []
    normalized_field_keys = set()
    for f in fields:
        raw_key = f.get("key") or f.get("fieldKey") or ""
        k = str(raw_key).strip().upper().replace("{{", "").replace("}}", "")
        normalized_field_keys.add({"FULL_NAME": "NAME", "PARTICIPANT_NAME": "NAME", "STUDENTNAME": "NAME"}.get(k, k))
    if "NAME" not in normalized_field_keys:
        raise RuntimeError("Certificate template has no Participant Name field. Add Participant Name and save the template before generating.")
    values = {
        "NAME": participant_name,
        "EMAIL": str(item.get("participantEmail") or ""),
        "STUDENT_ID": str(item.get("participantStudentId") or ""),
        "ROLL_NO": str(item.get("participantRollNumber") or ""),
        "EVENT_NAME": str(item.get("eventName") or ""),
        "DATE": str(item.get("issueDate") or ""),
        "CERTIFICATE_ID": str(item.get("certificateId") or ""),
    }
    source = io.BytesIO(stored_template["data"])
    extension = str(template.get("fileType") or "").lower()
    if extension == "pdf":
        reader = PdfReader(source)
        if not reader.pages:
            raise RuntimeError("Certificate template PDF has no pages.")
        base_page = reader.pages[0]
        width = float(base_page.mediabox.width)
        height = float(base_page.mediabox.height)
        packet = io.BytesIO()
        overlay = canvas.Canvas(packet, pagesize=(width, height))
        for field in fields:
            raw_key = field.get("key") or field.get("fieldKey") or ""
            key = str(raw_key).strip().upper().replace("{{", "").replace("}}", "")
            key = {"FULL_NAME": "NAME", "PARTICIPANT_NAME": "NAME", "STUDENTNAME": "NAME", "ROLLNUMBER": "ROLL_NO", "STUDENTID": "STUDENT_ID", "EVENT": "EVENT_NAME", "CERTIFICATEID": "CERTIFICATE_ID"}.get(key, key)
            _draw_field(overlay, field, values.get(key, ""), width, height)
        overlay.showPage()
        overlay.save()
        packet.seek(0)
        base_page.merge_page(PdfReader(packet).pages[0])
        writer = PdfWriter()
        writer.add_page(base_page)
        result = io.BytesIO()
        writer.write(result)
        output_bytes = result.getvalue()
    else:
        image_reader = ImageReader(source)
        iw, ih = image_reader.getSize()
        if not iw or not ih:
            raise RuntimeError("Certificate template image dimensions could not be read.")
        width = float(template.get("pageWidth") or 842.0)
        height = float(template.get("pageHeight") or (width * float(ih) / float(iw)))
        packet = io.BytesIO()
        overlay = canvas.Canvas(packet, pagesize=(width, height))
        overlay.drawImage(image_reader, 0, 0, width=width, height=height, preserveAspectRatio=False, mask="auto")
        for field in fields:
            raw_key = field.get("key") or field.get("fieldKey") or ""
            key = str(raw_key).strip().upper().replace("{{", "").replace("}}", "")
            key = {"FULL_NAME": "NAME", "PARTICIPANT_NAME": "NAME", "STUDENTNAME": "NAME", "ROLLNUMBER": "ROLL_NO", "STUDENTID": "STUDENT_ID", "EVENT": "EVENT_NAME", "CERTIFICATEID": "CERTIFICATE_ID"}.get(key, key)
            _draw_field(overlay, field, values.get(key, ""), width, height)
        overlay.showPage()
        overlay.save()
        packet.seek(0)
        output_bytes = packet.read()
    if not store:
        return output_bytes
    file_key = f"certificate:{item['certificateId']}"
    db.save_file(file_key, f"{item['certificateId']}.pdf", output_bytes, "application/pdf")
    return file_key


@certificate_api.post("/certificates/<certificate_id>/generate")
def generate_certificate(certificate_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = _find_certificate(certificate_id)
    template = next((t for t in portal_state["templates"] if t["id"] == item.get("templateId")), None) if item else None
    if not item or not template:
        return _error("NOT_FOUND", "Certificate or active template not found.", 404)
    if item["status"] not in ("APPROVED", "FAILED"):
        return _error("INVALID_STATE", "Certificate must be approved before generation.", 422)
    job_id = f"job_gen_{uuid.uuid4().hex[:12]}"
    try:
        file_key = _render_certificate(item, template)
        item.update({"status": "GENERATED", "generatedAt": _now(), "certificateUrl": f"/api/v1/jobs/{job_id}/file"})
        job = {"jobId": job_id, "type": "CERTIFICATE_GENERATION", "status": "COMPLETED", "progress": 100, "certificateUrl": item["certificateUrl"], "fileKey": file_key}
    except (OSError, RuntimeError, ValueError) as exc:
        item.update({"status": "FAILED", "failureReason": str(exc)})
        job = {"jobId": job_id, "type": "CERTIFICATE_GENERATION", "status": "FAILED", "progress": None, "certificateUrl": None, "error": str(exc)}
    portal_state.setdefault("jobs", []).append(job)
    _save_state()
    _audit("CERTIFICATE_GENERATED", item["certificateId"], "SUCCESS" if item["status"] == "GENERATED" else "FAILED", item.get("failureReason", ""))
    return _response({"certificateId": item["certificateId"], "status": item["status"], "jobId": job_id})


@certificate_api.get("/jobs/<job_id>")
def job_status(job_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = next((j for j in portal_state.get("jobs", []) if j["jobId"] == job_id), None)
    return _response({key: value for key, value in job.items() if key != "fileKey"}) if job else _error("NOT_FOUND", "Job not found.", 404)


@certificate_api.get("/jobs/<job_id>/file")
def job_file(job_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = next((j for j in portal_state.get("jobs", []) if j["jobId"] == job_id), None)
    if not job or not job.get("fileKey"):
        return _error("NOT_FOUND", "Generated certificate is not available.", 404)
    stored = db.load_file(job["fileKey"])
    if not stored:
        return _error("NOT_FOUND", "Generated certificate is not available.", 404)
    return send_file(io.BytesIO(stored["data"]), as_attachment=True,
                     download_name=stored.get("filename") or "certificate.pdf",
                     mimetype=stored.get("content_type") or "application/pdf")


def _render_email_content(item):
    """Render the exact subject/body that will be sent and logged."""
    settings = portal_state["settings"]
    participant = item.get("participant") if isinstance(item.get("participant"), dict) else {}
    name = str(item.get("participantName") or participant.get("name") or "").strip()
    email = str(item.get("participantEmail") or participant.get("email") or "").strip()
    student_id = str(item.get("participantStudentId") or participant.get("studentId") or "").strip()
    roll_no = str(item.get("participantRollNumber") or participant.get("rollNumber") or "").strip()
    values = {
        "{{NAME}}": name,
        "{{PARTICIPANT_NAME}}": name,
        "{{FULL_NAME}}": name,
        "{{EMAIL}}": email,
        "{{STUDENT_ID}}": student_id,
        "{{ROLL_NO}}": roll_no,
        "{{EVENT_NAME}}": str(item.get("eventName") or settings.get("eventName") or ""),
        "{{DATE}}": str(item.get("issueDate") or ""),
        "{{CERTIFICATE_ID}}": str(item.get("certificateId") or ""),
    }
    subject = str(settings.get("emailSubject") or "Your certificate")
    body = str(settings.get("emailBodyTemplate") or "Your certificate is attached.")
    for token, value in values.items():
        subject = subject.replace(token, value).replace(token.lower(), value)
        body = body.replace(token, value).replace(token.lower(), value)
    return subject, body


def _send_via_brevo(item, stored_certificate, subject, body):
    api_key = (os.getenv("BREVO_API_KEY") or "").strip()
    if not api_key:
        return False
    if requests is None:
        raise RuntimeError("The 'requests' package is required for Brevo email delivery.")
    sender_email = (os.getenv("BREVO_SENDER_EMAIL") or "").strip()
    if not sender_email:
        raise RuntimeError("BREVO_SENDER_EMAIL is not configured.")
    sender_name = os.getenv("BREVO_SENDER_NAME") or portal_state.get("settings", {}).get("senderName") or sender_email
    filename = stored_certificate.get("filename") or f"{item['certificateId']}.pdf"
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": item["participantEmail"]}],
        "subject": subject,
        "textContent": body,
        "attachment": [{"content": base64.b64encode(stored_certificate["data"]).decode("ascii"), "name": filename}],
    }
    reply_to = portal_state.get("settings", {}).get("replyToAddress")
    if reply_to:
        payload["replyTo"] = {"email": reply_to}
    try:
        response = requests.post(
            BREVO_SEND_URL,
            headers={"api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Brevo connection failed: {exc}") from exc
    if response.status_code >= 300:
        detail = response.text[:500]
        raise RuntimeError(f"Brevo API error ({response.status_code}): {detail}")
    return True


def _send_via_smtp(item, stored_certificate, subject, body):
    host, port = os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587"))
    if not host or not os.getenv("SMTP_USERNAME") or not os.getenv("SMTP_PASSWORD"):
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USERNAME"))
    message["To"] = item["participantEmail"]
    reply_to = os.getenv("SMTP_REPLY_TO") or portal_state.get("settings", {}).get("replyToAddress")
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)
    attachment_content_type = stored_certificate.get("content_type") or "application/pdf"
    maintype, _, subtype = attachment_content_type.partition("/")
    message.add_attachment(
        stored_certificate["data"],
        maintype=maintype or "application",
        subtype=subtype or "pdf",
        filename=stored_certificate.get("filename") or f"{item['certificateId']}.pdf",
    )
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD"))
        smtp.send_message(message)
    return True


def _deliver_certificate_email(item, stored_certificate, subject, body):
    """Send the already-rendered email through the configured provider."""
    if _send_via_brevo(item, stored_certificate, subject, body):
        return
    if _send_via_smtp(item, stored_certificate, subject, body):
        return
    raise RuntimeError("Email delivery is not configured.")




@certificate_api.post("/certificates/<certificate_id>/send")
def send_certificate(certificate_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = _find_certificate(certificate_id)
    if not item or item["status"] not in ("GENERATED", "FAILED", "SENT"):
        return _error("INVALID_STATE", "A generated certificate is required before sending.", 422)
    if not str(item.get("participantEmail") or "").strip() or "@" not in str(item.get("participantEmail") or ""):
        return _error("INVALID_EMAIL", "Participant email is missing or invalid.", 422)

    # A failed delivery can be retried using the same generated certificate.
    if item["status"] == "FAILED":
        item["status"] = "GENERATED"

    email_job_id = f"email_{uuid.uuid4().hex[:12]}"
    subject, body = _render_email_content(item)
    job = {
        "id": email_job_id,
        "jobId": email_job_id,
        "certificateId": item["certificateId"],
        "recipient": item["participantEmail"],
        "studentName": item.get("participantName", ""),
        "status": "PROCESSING",
        "createdAt": _now(),
        "sentAt": None,
        "attempts": 1,
        "subject": subject,
        "body": body,
        "attachmentFilename": None,
    }
    try:
        stored_certificate = db.load_file(f"certificate:{item['certificateId']}")
        if not stored_certificate:
            raise RuntimeError("Generated certificate file is not available to attach.")
        job["attachmentFilename"] = stored_certificate.get("filename") or f"{item['certificateId']}.pdf"
        _deliver_certificate_email(item, stored_certificate, subject, body)
        job.update({"status": "SENT", "sentAt": _now()})
        item.update({"status": "SENT", "sentAt": job["sentAt"], "emailDeliveryStatus": "SENT"})
        _audit("EMAIL_SENT", item["certificateId"])
    except (OSError, smtplib.SMTPException, RuntimeError) as exc:
        job.update({"status": "FAILED", "error": str(exc)})
        item.update({"status": "FAILED", "emailDeliveryStatus": "FAILED", "failureReason": str(exc)})
        _audit("EMAIL_FAILED", item["certificateId"], "FAILED", str(exc))

    portal_state["emailJobs"].append(job)
    _save_state()
    return _response({
        "certificateId": item["certificateId"],
        "status": item["status"],
        "emailJobId": email_job_id,
    })


@certificate_api.get("/email-jobs/<job_id>")
def email_job(job_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = next((j for j in portal_state["emailJobs"] if j["jobId"] == job_id), None)
    return _response(job) if job else _error("NOT_FOUND", "Email job not found.", 404)


@certificate_api.post("/email-jobs/<job_id>/retry")
def retry_email(job_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    job = next((j for j in portal_state["emailJobs"] if j["jobId"] == job_id), None)
    if not job:
        return _error("NOT_FOUND", "Email job not found.", 404)
    return send_certificate(job["certificateId"])


@certificate_api.get("/emails")
def email_logs():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    return _response(_paginate(portal_state["emailJobs"]))


@certificate_api.get("/dashboard/stats")
def dashboard_stats():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    certificates = portal_state["certificates"]
    return _response({
        "participants": len(portal_state["participants"]),
        "eligible": sum(p["eligibility"] == "ELIGIBLE" for p in portal_state["participants"]),
        "ineligible": sum(p["eligibility"] == "NOT_ELIGIBLE" for p in portal_state["participants"]),
        "pending": sum(p.get("eligibility") == "PENDING" for p in portal_state["participants"]),
        "pendingCertificates": sum(c["status"] == "PENDING" for c in certificates),
        "approved": sum(c["status"] == "APPROVED" for c in certificates),
        "rejected": sum(c["status"] == "REJECTED" for c in certificates),
        "generated": sum(c["status"] == "GENERATED" for c in certificates),
        "sent": sum(c["status"] == "SENT" for c in certificates),
        "failed": sum(c["status"] == "FAILED" for c in certificates),
    })


@certificate_api.get("/audit")
def audit_logs():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    return _response(_paginate(portal_state["auditLogs"]))


@certificate_api.get("/settings")
def get_settings():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    return _response(portal_state["settings"])


@certificate_api.put("/settings")
def update_settings():
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    body = request.get_json(silent=True) or {}
    blocked = {"smtpPassword", "apiKey", "databasePassword", "privateKey"}
    portal_state["settings"].update({key: value for key, value in body.items() if key not in blocked})
    _save_state()
    _audit("SETTINGS_UPDATED", "system settings")
    return _response(portal_state["settings"])


@certificate_api.get("/certificates/<certificate_id>/download")
def certificate_download(certificate_id):
    if not _require_admin():
        return _error("UNAUTHORIZED", "Authentication required.", 401)
    item = _find_certificate(certificate_id)
    if not item or item.get("status") not in ("GENERATED", "SENT"):
        return _error("NOT_FOUND", "Generated certificate is not available.", 404)
    stored = db.load_file(f"certificate:{item['certificateId']}")
    if not stored:
        return _error("NOT_FOUND", "Generated certificate file is not available.", 404)
    return send_file(io.BytesIO(stored["data"]), as_attachment=True,
                     download_name=stored.get("filename") or f"{item['certificateId']}.pdf",
                     mimetype=stored.get("content_type") or "application/pdf")


@certificate_api.get("/public/certificates/<certificate_id>/file")
def public_certificate_file(certificate_id):
    item = _find_certificate(certificate_id)
    if not item or item.get("status") not in ("GENERATED", "SENT"):
        return _error("NOT_FOUND", "Certificate is not publicly available.", 404)
    stored = db.load_file(f"certificate:{item['certificateId']}")
    if not stored:
        return _error("NOT_FOUND", "Certificate file is not available.", 404)
    return send_file(
        io.BytesIO(stored["data"]),
        download_name=stored.get("filename") or f"{item['certificateId']}.pdf",
        mimetype=stored.get("content_type") or "application/pdf",
    )


@certificate_api.get("/public/certificates/<certificate_id>/verify")
def verify_certificate(certificate_id):
    item = _find_certificate(certificate_id)
    if not item or item["status"] not in ("GENERATED", "SENT"):
        return _response({"valid": False, "isValid": False, "status": "NOT_FOUND", "certificateId": certificate_id,
                          "name": "", "recipientName": "", "eventName": "", "organization": "", "issuedAt": "", "issueDate": ""})
    issued_at = item.get("generatedAt") or item.get("issueDate") or ""
    return _response({"valid": True, "isValid": True, "certificateId": item["certificateId"],
                      "name": item["participantName"], "recipientName": item["participantName"],
                      "eventName": item["eventName"], "organization": portal_state["settings"]["organizationName"],
                      "issuedAt": issued_at, "issueDate": item.get("issueDate") or issued_at, "status": "VALID"})
