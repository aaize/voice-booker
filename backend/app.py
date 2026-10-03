"""Voice Booker backend — FastAPI + SQLite + Twilio voice webhooks.

Run:
  cd backend
  python -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  cp .env.example .env   # then add OPENAI_API_KEY / Twilio keys
  uvicorn app:app --reload --port 8000

Dashboard: open ../frontend/index.html (calls http://127.0.0.1:8000)
Twilio test without a phone: POST /api/simulate-call {"transcript": "..."}
Voicemail flow: missed calls hit POST /voice/voicemail-greeting (says + <Record>),
  recording posts to POST /voice/voicemail which transcribes (Whisper when
  OPENAI_API_KEY is set, else Twilio TranscriptionText) then auto-books via
  run_booking(source="voicemail").
Dashboard voicemail flow (two steps, no fake data):
  1. POST /api/voicemail/upload (or /api/voicemails/upload for several files)
     stores the audio as pending.
  2. POST /api/voicemail/{id}/extract for one file, or
     POST /api/voicemail/extract-all to run AI over every pending voicemail.
Legacy manual test: POST /api/voicemail/process {"transcript": "..."}
  or POST /api/simulate-call {"transcript": "..."}.
"""
import base64
import io
import os
import re
import sqlite3
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, os.environ.get("DATABASE", "voice.db"))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Voice Booker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


# ---------------- db ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS businesses (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          phone TEXT NOT NULL DEFAULT '',
          open_hour INTEGER NOT NULL DEFAULT 9,
          close_hour INTEGER NOT NULL DEFAULT 18,
          slot_min INTEGER NOT NULL DEFAULT 30
        );
        CREATE TABLE IF NOT EXISTS services (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          duration_min INTEGER NOT NULL DEFAULT 30,
          price_cents INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS appointments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          service_id INTEGER REFERENCES services(id) ON DELETE SET NULL,
          customer_name TEXT NOT NULL DEFAULT '',
          customer_phone TEXT NOT NULL DEFAULT '',
          starts_at TEXT NOT NULL,
          ends_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'booked',
          source TEXT NOT NULL DEFAULT 'dashboard',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          call_sid TEXT NOT NULL DEFAULT '',
          business_id INTEGER REFERENCES businesses(id) ON DELETE SET NULL,
          transcript TEXT NOT NULL DEFAULT '',
          agent_reply TEXT NOT NULL DEFAULT '',
          appointment_id INTEGER REFERENCES appointments(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS voicemails (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          call_sid TEXT NOT NULL DEFAULT '',
          recording_sid TEXT NOT NULL DEFAULT '',
          business_id INTEGER REFERENCES businesses(id) ON DELETE SET NULL,
          from_phone TEXT NOT NULL DEFAULT '',
          to_phone TEXT NOT NULL DEFAULT '',
          recording_url TEXT NOT NULL DEFAULT '',
          transcript TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'processed',
          appointment_id INTEGER REFERENCES appointments(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    con.commit()
    # lightweight migration for staged uploads (older DBs lack these columns)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(voicemails)").fetchall()}
    if "audio_path" not in cols:
        con.execute("ALTER TABLE voicemails ADD COLUMN audio_path TEXT NOT NULL DEFAULT ''")
    if "filename" not in cols:
        con.execute("ALTER TABLE voicemails ADD COLUMN filename TEXT NOT NULL DEFAULT ''")
    con.commit()
    if con.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"] == 0:
        cur = con.execute(
            "INSERT INTO businesses (name, phone, open_hour, close_hour) VALUES (?,?,?,?)",
            ("Glow Studio", os.environ.get("TWILIO_PHONE_NUMBER", ""), 9, 18),
        )
        bid = cur.lastrowid
        con.executemany(
            "INSERT INTO services (business_id, name, duration_min, price_cents) VALUES (?,?,?,?)",
            [
                (bid, "Haircut", 30, 3500),
                (bid, "Color", 90, 12000),
                (bid, "Consultation", 15, 0),
            ],
        )
        con.commit()
    con.close()


init_db()


def get_business(bid: int = 1):
    con = db()
    b = con.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone()
    con.close()
    return b


def business_by_phone(called: str):
    if not called:
        return get_business(1)
    con = db()
    b = con.execute("SELECT * FROM businesses WHERE phone = ?", (called,)).fetchone()
    con.close()
    return b or get_business(1)


def services_for(bid: int):
    con = db()
    rows = con.execute("SELECT * FROM services WHERE business_id = ?", (bid,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ---------------- booking brain ----------------
def extract_with_rules(transcript: str, service_names: list[str]) -> dict:
    t = (transcript or "").lower()
    out: dict = {"service": None, "date": None, "time": None, "name": None}

    for s in service_names:
        if s.lower() in t:
            out["service"] = s
            break

    if "today" in t:
        out["date"] = "today"
    elif "tomorrow" in t:
        out["date"] = "tomorrow"
    else:
        for day in WEEKDAYS:
            if day in t or day[:3] in re.findall(r"\b\w+\b", t):
                out["date"] = day
                break
        m = re.search(r"(\d{4}-\d{2}-\d{2})", t)
        if m:
            out["date"] = m.group(1)

    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        out["time"] = f"{h:02d}:{mi:02d}"
    elif "morning" in t:
        out["time"] = "10:00"
    elif "afternoon" in t:
        out["time"] = "14:00"
    elif "evening" in t:
        out["time"] = "17:00"

    m = re.search(r"(?:my name is|this is|i'm|i am)\s+([a-z]+(?:\s+[a-z]+)?)", t)
    if m:
        out["name"] = m.group(1).title()
    return out


def try_openai_extract(transcript: str, service_names: list[str]) -> dict | None:
    """Use an LLM for extraction when OPENAI_API_KEY is set. None = fall back to rules."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI

        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract booking info as JSON {service, date, time, name}. "
                        f"Services: {service_names}. date: today|tomorrow|weekday|YYYY-MM-DD. "
                        "time: HH:MM 24h. Null when unknown."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            max_tokens=200,
        )
        import json as _json

        data = _json.loads(resp.choices[0].message.content or "{}")
        return {
            "service": data.get("service"),
            "date": data.get("date"),
            "time": data.get("time"),
            "name": data.get("name"),
        }
    except Exception:
        return None  # never break a live call on LLM errors


def fetch_recording_bytes(recording_url: str) -> tuple[bytes | None, str]:
    """Download a Twilio recording. Returns (bytes, filename). None on failure.

    Twilio RecordingUrl without an extension returns mp3 by default when
    fetched with Accept: audio/mpeg, or you can append .mp3. We try the URL
    as-is first, then with .mp3 suffix.
    """
    if not recording_url:
        return None, "voicemail.mp3"
    candidates = [recording_url]
    if not re.search(r"\.(mp3|wav)$", recording_url, re.I):
        candidates.append(recording_url.rstrip("/") + ".mp3")
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"Accept": "audio/mpeg"})
            if sid and token:
                creds = base64.b64encode(f"{sid}:{token}".encode()).decode()
                req.add_header("Authorization", f"Basic {creds}")
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                if data and len(data) > 1000:
                    ext = "mp3" if "mp3" in url else "wav" if url.endswith(".wav") else "mp3"
                    return data, f"voicemail.{ext}"
        except Exception:
            continue
    return None, "voicemail.mp3"


def transcribe_audio_bytes(audio: bytes, filename: str = "voicemail.mp3") -> str | None:
    """Transcribe audio with OpenAI Whisper. None = unavailable/failed."""
    if not audio or not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI

        client = OpenAI()
        buf = io.BytesIO(audio)
        buf.name = filename
        resp = client.audio.transcriptions.create(model="whisper-1", file=buf)
        text = (getattr(resp, "text", "") or "").strip()
        return text or None
    except Exception:
        return None


def resolve_date(label: str | None, now: datetime) -> datetime:
    label = (label or "").lower()
    if label == "tomorrow":
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if label == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", label or "")
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if label in WEEKDAYS:
        delta = (WEEKDAYS[label] - now.weekday()) % 7 or 7
        return (now + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
    # default: tomorrow
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def free_slots(business_id: int, day: datetime, duration_min: int) -> list[datetime]:
    b = get_business(business_id)
    open_h, close_h = b["open_hour"], b["close_hour"]
    step = b["slot_min"]
    con = db()
    appts = con.execute(
        "SELECT starts_at, ends_at FROM appointments WHERE business_id = ? AND status = 'booked'",
        (business_id,),
    ).fetchall()
    con.close()
    busy = [
        (datetime.fromisoformat(r["starts_at"]), datetime.fromisoformat(r["ends_at"]))
        for r in appts
    ]

    slots = []
    cur = day.replace(hour=open_h, minute=0, second=0, microsecond=0)
    end = day.replace(hour=close_h, minute=0, second=0, microsecond=0)
    while cur + timedelta(minutes=duration_min) <= end:
        slot_end = cur + timedelta(minutes=duration_min)
        if not any(cur < be and slot_end > bs for bs, be in busy) and cur > datetime.now():
            slots.append(cur)
        cur += timedelta(minutes=step)
    return slots


def run_booking(
    business_id: int, transcript: str, caller_phone: str = "", call_sid: str = "", source: str = "voice"
) -> dict:
    """Shared by /voice/process and /api/simulate-call. Returns {reply, appointment, parsed}."""
    b = get_business(business_id)
    svcs = services_for(business_id)
    names = [s["name"] for s in svcs]

    parsed = try_openai_extract(transcript, names) or {}
    if not any(parsed.values()):
        parsed = extract_with_rules(transcript, names)
    else:  # fill gaps with rules
        fb = extract_with_rules(transcript, names)
        for k in ("service", "date", "time", "name"):
            parsed.setdefault(k, fb.get(k))

    svc = next((s for s in svcs if s["name"] == parsed.get("service")), svcs[0] if svcs else None)
    if not svc:
        return {"reply": "Sorry, no bookable services are set up yet.", "appointment": None, "parsed": parsed}

    now = datetime.now()
    day = resolve_date(parsed.get("date"), now)
    hh, mm = 10, 0
    if parsed.get("time"):
        try:
            hh, mm = map(int, parsed["time"].split(":"))
        except ValueError:
            pass
    want = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if want <= now:
        want = now + timedelta(hours=2)

    slots = free_slots(business_id, want, svc["duration_min"])
    # snap to requested time if free, else nearest future slot
    chosen = min(slots, key=lambda s: abs((s - want).total_seconds()), default=None)
    if not chosen:  # try next day
        nxt = day + timedelta(days=1)
        slots = free_slots(business_id, nxt, svc["duration_min"])
        chosen = slots[0] if slots else None
    if not chosen:
        reply = "Sorry, we're fully booked the next couple of days. Please try calling back later."
        return {"reply": reply, "appointment": None, "parsed": parsed}

    name = parsed.get("name") or "Phone Guest"
    phone = caller_phone or ""
    m = re.search(r"(\+?\d[\d\s\-]{7,}\d)", transcript)
    if m:
        phone = re.sub(r"[\s\-]", "", m.group(1))

    ends = chosen + timedelta(minutes=svc["duration_min"])
    con = db()
    cur = con.execute(
        """INSERT INTO appointments
           (business_id, service_id, customer_name, customer_phone, starts_at, ends_at, status, source, created_at)
           VALUES (?,?,?,?,?,?, 'booked', ?, ?)""",
        (
            business_id, svc["id"], name, phone,
            chosen.isoformat(timespec="minutes"), ends.isoformat(timespec="minutes"),
            source, datetime.now().isoformat(timespec="seconds"),
        ),
    )
    appt_id = cur.lastrowid
    reply = (
        f"Got it {name}. You're booked for {svc['name']} on "
        f"{chosen.strftime('%A %B %d at %-I:%M %p')}. We'll text {phone or 'you'} a confirmation. Thanks for calling {b['name']}!"
    )
    con.execute(
        "INSERT INTO calls (call_sid, business_id, transcript, agent_reply, appointment_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (call_sid, business_id, transcript, reply, appt_id, datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    appt = con.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
    con.close()
    return {"reply": reply, "appointment": dict(appt), "parsed": parsed}


def process_voicemail(
    business_id: int = 1,
    transcript: str = "",
    audio_bytes: bytes | None = None,
    audio_filename: str = "voicemail.mp3",
    recording_url: str = "",
    caller_phone: str = "",
    call_sid: str = "",
    recording_sid: str = "",
    to_phone: str = "",
) -> dict:
    """Voicemail in -> text out -> booking in. Returns full result dict.

    Transcript resolution order:
      1. explicit `transcript` argument (dashboard paste or Twilio TranscriptionText)
      2. Whisper transcription of `audio_bytes` (uploaded file)
      3. download + Whisper transcription of `recording_url` (Twilio Record callback)
    Then runs run_booking(source="voicemail") and stores a voicemails row.
    Works offline (rules extraction) when no OPENAI_API_KEY is set, as long as
    a transcript is supplied directly.
    """
    # resolve business from the dialed number when caller didn't pass an id
    if to_phone:
        try:
            b = business_by_phone(to_phone)
            if b:
                business_id = b["id"]
        except Exception:
            pass

    text = (transcript or "").strip()
    transcribed = False
    if not text and audio_bytes:
        t = transcribe_audio_bytes(audio_bytes, audio_filename)
        if t:
            text, transcribed = t, True
    if not text and recording_url:
        blob, fname = fetch_recording_bytes(recording_url)
        if blob:
            t = transcribe_audio_bytes(blob, fname)
            if t:
                text, transcribed = t, True

    con = db()
    if not text:
        cur = con.execute(
            """INSERT INTO voicemails
               (call_sid, recording_sid, business_id, from_phone, to_phone,
                recording_url, transcript, status, appointment_id, created_at)
               VALUES (?,?,?,?,?,?, '', 'failed-no-transcript', NULL, ?)""",
            (call_sid, recording_sid, business_id, caller_phone, to_phone,
             recording_url, datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        vm_id = cur.lastrowid
        con.close()
        hint = "voicemail audio could not be transcribed"
        if not os.environ.get("OPENAI_API_KEY"):
            hint += " (set OPENAI_API_KEY for Whisper, or post transcript text directly)"
        return {
            "ok": False, "error": hint, "transcript": "",
            "transcribed": transcribed, "reply": None,
            "appointment": None, "parsed": None, "voicemail_id": vm_id,
        }

    result = run_booking(business_id, text, caller_phone=caller_phone,
                         call_sid=call_sid, source="voicemail")
    status = "processed" if result.get("appointment") else "failed-no-slots"
    cur = con.execute(
        """INSERT INTO voicemails
           (call_sid, recording_sid, business_id, from_phone, to_phone,
            recording_url, transcript, status, appointment_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (call_sid, recording_sid, business_id, caller_phone, to_phone,
         recording_url, text, status,
         result["appointment"]["id"] if result.get("appointment") else None,
         datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    vm_id = cur.lastrowid
    con.close()
    return {"ok": True, "transcript": text, "transcribed": transcribed,
            **result, "voicemail_id": vm_id}


def safe_filename(name: str) -> str:
    name = (name or "voicemail").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "voicemail"
    return name[:80]


def stage_voicemail_upload(
    business_id: int = 1, file_bytes: bytes = b"",
    filename: str = "voicemail.mp3", caller_phone: str = "",
) -> dict:
    """Step 1 of the dashboard flow: store the uploaded file, no AI yet.

    Returns the voicemail row with status='pending'. The dashboard then runs
    step 2 (AI extract) per file or for all pending files at once.
    """
    fname = safe_filename(filename)
    stored = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:8]}_{fname}"
    audio_path = os.path.join(UPLOAD_DIR, stored)
    with open(audio_path, "wb") as f:
        f.write(file_bytes)
    con = db()
    cur = con.execute(
        """INSERT INTO voicemails
           (call_sid, recording_sid, business_id, from_phone, to_phone,
            recording_url, transcript, status, appointment_id,
            audio_path, filename, created_at)
           VALUES ('','',?,?, '', '', '', 'pending', NULL, ?, ?, ?)""",
        (business_id, caller_phone, audio_path, fname,
         datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    row = con.execute("SELECT * FROM voicemails WHERE id = ?", (cur.lastrowid,)).fetchone()
    con.close()
    return {"ok": True, "status": "pending", "voicemail": dict(row)}


def extract_voicemail(vm_id: int) -> dict:
    """Step 2 of the dashboard flow: AI transcribe + extract + book one file."""
    con = db()
    row = con.execute("SELECT * FROM voicemails WHERE id = ?", (vm_id,)).fetchone()
    con.close()
    if not row:
        return {"ok": False, "error": "voicemail not found", "voicemail_id": vm_id}
    vm = dict(row)

    text = (vm.get("transcript") or "").strip()
    transcribed = False
    if not text:
        blob = None
        fname = vm.get("filename") or "voicemail.mp3"
        if vm.get("audio_path") and os.path.exists(vm["audio_path"]):
            with open(vm["audio_path"], "rb") as f:
                blob = f.read()
        elif vm.get("recording_url"):
            blob, fname = fetch_recording_bytes(vm["recording_url"])
        if blob:
            t = transcribe_audio_bytes(blob, fname)
            if t:
                text, transcribed = t, True

    con = db()
    if not text:
        con.execute("UPDATE voicemails SET status = 'failed-no-transcript' WHERE id = ?", (vm_id,))
        con.commit()
        con.close()
        hint = "audio could not be transcribed"
        if not os.environ.get("OPENAI_API_KEY"):
            hint += " — set OPENAI_API_KEY in backend/.env to enable Whisper transcription"
        return {"ok": False, "error": hint, "transcript": "",
                "transcribed": transcribed, "voicemail_id": vm_id}

    result = run_booking(int(vm.get("business_id") or 1), text,
                         caller_phone=vm.get("from_phone") or "",
                         call_sid=vm.get("call_sid") or "", source="voicemail")
    status = "processed" if result.get("appointment") else "failed-no-slots"
    con.execute(
        "UPDATE voicemails SET transcript = ?, status = ?, appointment_id = ? WHERE id = ?",
        (text, status,
         result["appointment"]["id"] if result.get("appointment") else None, vm_id),
    )
    con.commit()
    updated = con.execute("SELECT * FROM voicemails WHERE id = ?", (vm_id,)).fetchone()
    con.close()
    return {"ok": True, "transcript": text, "transcribed": transcribed,
            **result, "voicemail": dict(updated), "voicemail_id": vm_id}


def extract_pending_voicemails(business_id: int = 1) -> dict:
    """One AI button: go through every pending/failed voicemail and extract info."""
    con = db()
    rows = con.execute(
        "SELECT id FROM voicemails WHERE business_id = ? AND status IN ('pending', 'failed-no-transcript')"
        " ORDER BY id ASC",
        (business_id,),
    ).fetchall()
    con.close()
    results = [extract_voicemail(r["id"]) for r in rows]
    done = sum(1 for r in results if r.get("ok") and r.get("appointment"))
    return {"ok": True, "processed": len(results), "booked": done, "results": results}


def twiml_say_gather(say: str, action: str = "/voice/process") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{say}</Say>"
        f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto" />'
        "<Say>We didn't hear anything. Goodbye!</Say>"
        "</Response>"
    )


# ---------------- api ----------------
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "ai": "openai" if os.environ.get("OPENAI_API_KEY") else "offline-rules",
        "stt": "whisper" if os.environ.get("OPENAI_API_KEY") else "transcript-only",
        "twilio": bool(os.environ.get("TWILIO_ACCOUNT_SID")),
    }


@app.get("/api/businesses")
def list_businesses():
    con = db()
    rows = con.execute("SELECT * FROM businesses").fetchall()
    con.close()
    return {"businesses": [dict(r) for r in rows]}


@app.get("/api/services")
def list_services(business_id: int = 1):
    return {"services": services_for(business_id)}


@app.get("/api/appointments")
def list_appointments(business_id: int = 1):
    con = db()
    rows = con.execute(
        "SELECT a.*, s.name AS service_name FROM appointments a"
        " LEFT JOIN services s ON s.id = a.service_id"
        " WHERE a.business_id = ? ORDER BY a.starts_at ASC LIMIT 200",
        (business_id,),
    ).fetchall()
    con.close()
    return {"appointments": [dict(r) for r in rows]}


@app.post("/api/appointments")
async def create_appointment(req: Request):
    data = await req.json()
    con = db()
    svc = con.execute("SELECT * FROM services WHERE id = ?", (data.get("service_id"),)).fetchone()
    dur = svc["duration_min"] if svc else 30
    start = datetime.fromisoformat(data["starts_at"])
    end = start + timedelta(minutes=dur)
    cur = con.execute(
        """INSERT INTO appointments
           (business_id, service_id, customer_name, customer_phone, starts_at, ends_at, status, source, created_at)
           VALUES (?,?,?,?,?,?, 'booked', 'dashboard', ?)""",
        (
            data.get("business_id", 1), data.get("service_id"),
            data.get("customer_name", ""), data.get("customer_phone", ""),
            start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    con.commit()
    row = con.execute("SELECT * FROM appointments WHERE id = ?", (cur.lastrowid,)).fetchone()
    con.close()
    return {"appointment": dict(row)}


@app.delete("/api/appointments/{appt_id}")
def cancel_appointment(appt_id: int):
    con = db()
    con.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt_id,))
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/api/availability")
def availability(business_id: int = 1, date: str = ""):
    try:
        day = datetime.fromisoformat(date) if date else datetime.now() + timedelta(days=1)
    except ValueError:
        day = datetime.now() + timedelta(days=1)
    slots = free_slots(business_id, day, 30)
    return {"date": day.date().isoformat(), "slots": [s.isoformat(timespec="minutes") for s in slots]}


@app.post("/api/simulate-call")
async def simulate_call(req: Request):
    """Test the voice agent without Twilio/phones."""
    data = await req.json()
    result = run_booking(
        int(data.get("business_id", 1)),
        data.get("transcript", ""),
        data.get("caller_phone", ""),
        source="simulator",
    )
    return result


@app.get("/api/voicemails")
def list_voicemails(business_id: int = 1):
    con = db()
    rows = con.execute(
        "SELECT v.*, a.starts_at AS appointment_starts_at, a.customer_name AS booked_name,"
        " s.name AS service_name FROM voicemails v"
        " LEFT JOIN appointments a ON a.id = v.appointment_id"
        " LEFT JOIN services s ON s.id = a.service_id"
        " WHERE v.business_id = ? ORDER BY v.id DESC LIMIT 200",
        (business_id,),
    ).fetchall()
    con.close()
    return {"voicemails": [dict(r) for r in rows]}


@app.post("/api/voicemail/process")
async def voicemail_process(req: Request):
    """Manual voicemail ingest: {transcript} or {recording_url} -> transcribe -> book."""
    data = await req.json()
    result = process_voicemail(
        business_id=int(data.get("business_id", 1)),
        transcript=data.get("transcript", "") or data.get("TranscriptionText", ""),
        recording_url=data.get("recording_url", "") or data.get("RecordingUrl", ""),
        caller_phone=data.get("caller_phone", "") or data.get("From", ""),
        call_sid=data.get("call_sid", "") or data.get("CallSid", ""),
        recording_sid=data.get("recording_sid", "") or data.get("RecordingSid", ""),
        to_phone=data.get("to_phone", "") or data.get("To", ""),
    )
    return result


@app.post("/api/voicemail/upload")
async def voicemail_upload(
    file: UploadFile = File(...), business_id: int = 1, caller_phone: str = Form(default="")
):
    """Step 1: upload a voicemail file. Stored as pending — no AI yet.

    Run step 2 with POST /api/voicemail/{id}/extract (one file) or
    POST /api/voicemail/extract-all (every pending file).
    """
    audio = await file.read()
    if not audio:
        return {"ok": False, "error": "empty file"}
    return stage_voicemail_upload(
        business_id=business_id,
        file_bytes=audio,
        filename=file.filename or "voicemail.mp3",
        caller_phone=caller_phone,
    )


@app.post("/api/voicemails/upload")
async def voicemails_upload(
    files: list[UploadFile] = File(...), business_id: int = 1,
):
    """Step 1 for several files at once. Each stored as pending."""
    out = []
    for f in files:
        audio = await f.read()
        if not audio:
            out.append({"ok": False, "error": "empty file", "filename": f.filename})
            continue
        out.append(stage_voicemail_upload(
            business_id=business_id, file_bytes=audio,
            filename=f.filename or "voicemail.mp3"))
    return {"ok": True, "uploaded": len(out), "results": out}


@app.post("/api/voicemail/{vm_id}/extract")
def voicemail_extract(vm_id: int):
    """Step 2 for one file: AI transcribe -> extract booking info -> book."""
    return extract_voicemail(vm_id)


@app.post("/api/voicemail/extract-all")
async def voicemail_extract_all(req: Request):
    """One AI button: go through every pending voicemail and extract info."""
    try:
        data = await req.json()
    except Exception:
        data = {}
    return extract_pending_voicemails(int(data.get("business_id", 1)))


@app.get("/api/voicemail/{vm_id}/audio")
def voicemail_audio(vm_id: int):
    """Stream the stored voicemail audio for in-browser playback."""
    con = db()
    row = con.execute("SELECT audio_path, filename FROM voicemails WHERE id = ?",
                      (vm_id,)).fetchone()
    con.close()
    if not row or not row["audio_path"] or not os.path.exists(row["audio_path"]):
        return PlainTextResponse("audio not found", status_code=404)
    return FileResponse(row["audio_path"], filename=row["filename"] or "voicemail.mp3")


@app.delete("/api/voicemail/{vm_id}")
def voicemail_delete(vm_id: int):
    con = db()
    row = con.execute("SELECT audio_path FROM voicemails WHERE id = ?", (vm_id,)).fetchone()
    if row and row["audio_path"] and os.path.exists(row["audio_path"]):
        try:
            os.remove(row["audio_path"])
        except OSError:
            pass
    con.execute("DELETE FROM voicemails WHERE id = ?", (vm_id,))
    con.commit()
    con.close()
    return {"ok": True}


# ---------------- Twilio voice ----------------
@app.post("/voice/incoming")
async def voice_incoming(To: str = Form(default=""), From: str = Form(default="")):
    b = business_by_phone(To)
    msg = (
        f"Thanks for calling {b['name']}. Tell me which service you want, "
        "and what day and time works for you. For example, say haircut tomorrow at 3 p m."
    )
    return PlainTextResponse(twiml_say_gather(msg), media_type="application/xml")


@app.post("/voice/process")
async def voice_process(
    SpeechResult: str = Form(default=""),
    CallSid: str = Form(default=""),
    To: str = Form(default=""),
    From: str = Form(default=""),
):
    b = business_by_phone(To)
    if not SpeechResult.strip():
        return PlainTextResponse(
            twiml_say_gather("Sorry, I didn't catch that. What day and time works for you?"),
            media_type="application/xml",
        )
    result = run_booking(b["id"], SpeechResult, caller_phone=From, call_sid=CallSid)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{result['reply']}</Say><Hangup/></Response>"
    )
    return PlainTextResponse(xml, media_type="application/xml")


def twiml_record_voicemail(business_name: str, action: str = "/voice/voicemail") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>Thanks for calling {business_name}. We're away right now. "
        "Please leave your name, the service you want, and what day and time works for you, "
        "after the beep. We'll text you a confirmation.</Say>"
        f'<Record action="{action}" method="POST" maxLength="120" playBeep="true" '
        'trim="trim-silence" recordingStatusCallback="/voice/voicemail" />'
        "<Say>We didn't get your message. Goodbye!</Say>"
        "</Response>"
    )


# ---------------- Twilio voicemail ----------------
@app.post("/voice/voicemail-greeting")
async def voice_voicemail_greeting(To: str = Form(default="")):
    """Missed-call handler: point Twilio's busy/no-answer webhook here."""
    b = business_by_phone(To)
    return PlainTextResponse(twiml_record_voicemail(b["name"]), media_type="application/xml")


@app.post("/voice/voicemail")
async def voice_voicemail(
    RecordingUrl: str = Form(default=""),
    RecordingSid: str = Form(default=""),
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default=""),
    TranscriptionText: str = Form(default=""),
    TranscriptionStatus: str = Form(default=""),
):
    """Twilio <Record> callback: transcribe the voicemail, extract booking, save it.

    Works with or without Twilio's own transcription: if TranscriptionText is
    present we use it directly (offline-friendly); otherwise we download the
    recording and run Whisper when OPENAI_API_KEY is set.
    """
    b = business_by_phone(To)
    result = process_voicemail(
        business_id=b["id"],
        transcript=TranscriptionText,
        recording_url=RecordingUrl,
        caller_phone=From,
        call_sid=CallSid,
        recording_sid=RecordingSid,
        to_phone=To,
    )
    if result.get("appointment"):
        say = result["reply"]
    elif not result.get("transcript"):
        say = ("Thanks for your message. Sorry, we couldn't hear it clearly. "
               "Please call back with the service, day and time you want.")
    else:
        say = ("Thanks for your message. Sorry, we couldn't find a free slot. "
               "We'll call you back to arrange a time.")
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{say}</Say><Hangup/></Response>'
    return PlainTextResponse(xml, media_type="application/xml")
