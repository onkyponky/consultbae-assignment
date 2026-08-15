"""Phase 3 audio collection app.

Two views and a submit handler:

    GET  /              the form: name, phone, record or upload
    POST /submit        validate, store, extract metadata, save a row
    GET  /submissions   every submission with its extracted properties
    GET  /audio/{id}    stream one stored file back, looked up by row id

The point of the person link is that this is the SAME database Task 1
built. A submission finds its person by running the submitted phone through
`normalise_phone` -- the identical function and identical key the Phase 2
merge used to bridge source1 to source3. No second matching rule exists.

Security notes that shaped the code:

  * The browser's filename never reaches a path. Files are stored under a
    uuid4 we generate, and served by database id, so a crafted filename has
    nothing to act on.
  * Everything is validated before anything is written. A rejected
    submission stores no file and creates no person.

    python -m uvicorn app:app --reload --app-dir src
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from audio_meta import extract
from models import AudioSubmission, Base, DataIssue, Person
from normalise import normalise_email, normalise_phone

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
DB_PATH = REPO_ROOT / "db" / "consultbae.db"
UPLOAD_DIR = REPO_ROOT / "uploads"
TEMPLATE_DIR = SRC_DIR / "templates"

#: 25 MB. Long enough for several minutes of voice, small enough that a
#: single request cannot exhaust disk or memory.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: What a browser actually sends. MediaRecorder produces audio/webm in
#: Chrome and Firefox and audio/mp4 in Safari; the rest cover file uploads.
#: Extensions are chosen from THIS table, never from the uploaded filename.
ALLOWED_TYPES: dict[str, str] = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}

app = FastAPI(title="ConsultBae audio collection")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

engine = create_engine(f"sqlite:///{DB_PATH}")


def get_session() -> Session:
    """One session per request. Small app, no pooling subtleties needed."""
    return Session(engine)


@app.on_event("startup")
def prepare_storage() -> None:
    """Make sure the tables and the upload directory exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


class Rejected(Exception):
    """A submission that fails a rule. The message is shown to the user."""


def validate_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise Rejected("Please enter your name.")
    if len(name) > 200:
        raise Rejected("That name is too long (200 characters maximum).")
    return name


def validate_phone(raw: str) -> tuple[str, str]:
    """Return (as typed, normalised key), or explain why it cannot be used.

    The key comes from `normalise_phone`, which returns None below 10
    digits. Rather than invent a second rule for the app, a phone the merge
    could not use is a phone this form will not accept.
    """
    typed = (raw or "").strip()
    if not typed:
        raise Rejected("Please enter your phone number.")

    key = normalise_phone(typed)
    if key is None:
        raise Rejected(
            "That phone number is too short. Please enter at least 10 digits, "
            "for example 9000000254 or +91-9000000254."
        )
    return typed, key


def validate_audio(upload: UploadFile | None) -> tuple[bytes, str, str]:
    """Return (file bytes, content type, extension) or explain the rejection."""
    if upload is None or not upload.filename:
        raise Rejected("Please record a clip or choose an audio file to upload.")

    content_type = (upload.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_TYPES:
        # List file extensions, not MIME subtypes: "x-m4a" and "wave" are
        # not things a person recognises as a file format.
        allowed = ", ".join(sorted({ext.lstrip(".") for ext in ALLOWED_TYPES.values()}))
        raise Rejected(
            f"That file type ({content_type or 'unknown'}) is not accepted. "
            f"Allowed formats: {allowed}."
        )

    payload = upload.file.read()
    if not payload:
        raise Rejected("That file is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        megabytes = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise Rejected(f"That file is too large. The limit is {megabytes} MB.")

    return payload, content_type, ALLOWED_TYPES[content_type]


# --------------------------------------------------------------------------
# Person lookup -- reusing Phase 2's rule, not a new one
# --------------------------------------------------------------------------


def find_or_create_person(session: Session, name: str, typed: str, key: str) -> Person:
    """Attach the submission to a person, creating one only if needed."""
    matches = list(
        session.scalars(select(Person).where(Person.primary_phone == key).order_by(Person.id)).all()
    )

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        # After the merge, primary_phone is unique among non-null values,
        # but nothing in the schema enforces that, so do not assume it.
        session.add(
            DataIssue(
                source_file="audio_app",
                row_number=None,
                issue_type="ambiguous_phone_on_audio_submission",
                column_name="phone",
                raw_value=typed,
                action_taken=(
                    f"Phone {key} matched {len(matches)} people "
                    f"(ids {[p.id for p in matches]}). Attached the submission "
                    "to the lowest id. This needs review: the merge should "
                    "leave primary_phone unique."
                ),
                severity="flagged",
            )
        )
        return matches[0]

    person = Person(
        full_name=name,
        primary_phone=key,
        source_origin="audio_app",
    )
    session.add(person)
    session.flush()

    session.add(
        DataIssue(
            source_file="audio_app",
            row_number=None,
            issue_type="person_created_from_audio_submission",
            column_name="phone",
            raw_value=typed,
            action_taken=(
                f"No person had phone {key}, so person id {person.id} was "
                f"created from the submitted name {name!r} with "
                "source_origin='audio_app'. Rejecting the submission instead "
                "would break the requirement that every recording lands in "
                "the database."
            ),
            severity="flagged",
        )
    )
    return person


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"error": None})


@app.post("/submit")
def submit(
    request: Request,
    name: str = Form(""),
    phone: str = Form(""),
    capture_mode: str = Form("upload"),
    audio: UploadFile | None = None,
):
    """Validate everything, then store. Nothing is written before it passes."""
    try:
        clean_name = validate_name(name)
        typed_phone, phone_key = validate_phone(phone)
        payload, content_type, extension = validate_audio(audio)
    except Rejected as rejection:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"error": str(rejection)},
            status_code=400,
        )

    if capture_mode not in {"recording", "upload"}:
        capture_mode = "upload"

    # Store under a name we generate. The browser's filename is recorded on
    # the row but never used to build this path.
    stored_filename = f"{uuid.uuid4().hex}{extension}"
    destination = UPLOAD_DIR / stored_filename
    destination.write_bytes(payload)

    metadata = extract(destination)

    with get_session() as session:
        person = find_or_create_person(session, clean_name, typed_phone, phone_key)

        session.add(
            AudioSubmission(
                person_id=person.id,
                submitted_name=clean_name,
                submitted_phone=typed_phone,
                matched_phone=phone_key,
                stored_filename=stored_filename,
                original_filename=audio.filename,
                content_type=content_type,
                byte_size=metadata.byte_size,
                capture_mode=capture_mode,
                duration_seconds=metadata.duration_seconds,
                sample_rate_hz=metadata.sample_rate_hz,
                bitrate_bps=metadata.bitrate_bps,
                bitrate_is_derived=metadata.bitrate_is_derived,
                bitrate_note=metadata.bitrate_note,
                loudness_lufs=metadata.loudness_lufs,
                rms_level_db=metadata.rms_level_db,
                noise_snr_db=metadata.noise_snr_db,
                quality_estimate=metadata.quality_estimate,
                probe_error="; ".join(metadata.problems) or None,
            )
        )
        session.commit()

    return RedirectResponse(url="/submissions", status_code=303)


@app.get("/submissions", response_class=HTMLResponse)
def submissions(request: Request):
    """Every submission, newest first, with the person it belongs to."""
    with get_session() as session:
        rows = session.execute(
            select(AudioSubmission, Person)
            .join(Person, AudioSubmission.person_id == Person.id)
            .order_by(AudioSubmission.id.desc())
        ).all()

        return templates.TemplateResponse(
            request,
            "submissions.html",
            {"rows": rows},
        )


@app.get("/api/lookup")
def api_lookup(phone: str | None = None, email: str | None = None):
    """Read-only: does a person with this phone or email already exist?

    This exists so the n8n duplicate-check flow can ask the database a
    question. It answers ONLY that question -- it decides nothing, writes
    nothing, and sends no alert. Deciding what a hit means and who to tell
    is the flow's job, which is the whole point of building Task 2 in a
    no-code tool rather than in Python.

    Matching reuses `normalise_phone` and `normalise_email` from Phase 2.
    No matching rule is reimplemented here.

    Phone is tried before email because phone is the stronger key: it is
    what caught the `alt.nikhil.chopra70@` duplicate that email-only
    matching missed during the merge.
    """
    if phone is None and email is None:
        return JSONResponse(
            {"error": "Pass at least one of phone or email."}, status_code=400
        )

    phone_key = normalise_phone(phone) if phone else None
    email_key = normalise_email(email) if email else None

    answer: dict = {
        "query": {"phone": phone, "email": email},
        "normalised": {"phone": phone_key, "email": email_key},
        "found": False,
        "matched_on": None,
        "person": None,
    }

    if phone_key is None and email_key is None:
        # A row that carried values but none of them usable. Reported as a
        # miss with a note rather than a 400, so one unusable row does not
        # abort the whole CSV run in n8n.
        answer["note"] = "Neither phone nor email was usable as a key."
        return answer

    with get_session() as session:
        person = None
        if phone_key:
            person = session.scalar(
                select(Person).where(Person.primary_phone == phone_key).order_by(Person.id)
            )
            if person is not None:
                answer["matched_on"] = "phone"

        if person is None and email_key:
            person = session.scalar(
                select(Person).where(Person.primary_email == email_key).order_by(Person.id)
            )
            if person is not None:
                answer["matched_on"] = "email"

        if person is not None:
            answer["found"] = True
            answer["person"] = {
                "id": person.id,
                "full_name": person.full_name,
                "source_origin": person.source_origin,
            }

    return answer


@app.get("/audio/{submission_id}")
def audio_file(submission_id: int):
    """Serve a stored file by row id, never by a name from the URL."""
    with get_session() as session:
        submission = session.get(AudioSubmission, submission_id)
        if submission is None:
            return HTMLResponse("No such submission.", status_code=404)

        # Defensive: the stored name is a uuid we generated, so it should
        # never contain a separator. Refuse it outright if it somehow does.
        name = submission.stored_filename
        if "/" in name or "\\" in name or name.startswith("."):
            return HTMLResponse("Refusing to serve that path.", status_code=400)

        path = UPLOAD_DIR / name
        if not path.is_file():
            return HTMLResponse("That file is missing from storage.", status_code=404)

        return FileResponse(
            path,
            media_type=submission.content_type or "application/octet-stream",
        )
