"""Voice Booker — voicemail in, booking out.

Flow:
  1. Upload voicemail audio files in the dashboard
     (POST /api/voicemail/upload or /api/voicemails/upload for several).
     Files are stored, nothing else happens yet — status "pending".
  2. Press AI Extract on one file (POST /api/voicemail/{id}/extract) or
     AI Extract All (POST /api/voicemail/extract-all). The AI transcribes
     the conversation, pulls out service + day + time + name, and books it.

Transcription needs no API keys: it runs faster-whisper on this machine.
If you set OPENAI_API_KEY it is used as a cloud fallback (and for smarter
extraction). Without either, the API tells you exactly what is missing.

Phone (Twilio) flow still included:
  Voice webhook      POST /voice/incoming            (live answer)
  No-answer webhook  POST /voice/voicemail-greeting  (records a message)
  Record callback    POST /voice/voicemail           (transcribe + book)

Run:
  cd backend
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  uvicorn app:app --host 127.0.0.1 --port 8000
Dashboard: serve ../frontend (e.g. python3 -m http.server 5502) and open it.
"""
import base64
import io
import os
import re
import sqlite3
import urllib.request
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

load_dotenv()

# ---------------------------------------------------------------- config ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, os.environ.get("DATABASE", "voice.db"))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "base")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()

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

PENDING_STATUSES = ("pending", "failed-no-transcript")


# ------------------------------------------------------------------- db ---
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
          status TEXT NOT NULL DEFAULT 'pending',
          appointment_id INTEGER REFERENCES appointments(id) ON DELETE SET NULL,
          audio_path TEXT NOT NULL DEFAULT '',
          filename TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        """
    )
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


# ------------------------------------------------------- booking brain ---
def match_service(transcript: str, service_names: list) -> str | None:
    """Find the mentioned service, tolerating STT mishearings.

    1. exact mention ("we do haircut")
    2. spacing variants ("hair cut" matches "Haircut")
    3. short form ("cut" matches "Haircut", "consult" matches "Consultation")
    4. fuzzy word match ("hair kit", "colour" match via difflib)
    First service in list wins ties; None when nothing is close.
    """
    import difflib

    t = (transcript or "").lower()
    words = re.findall(r"[a-z]+", t)
    flat = "".join(words)

    for s in service_names:
        if s.lower() in t:
            return s
    compacts = {s: re.sub(r"[^a-z]", "", s.lower()) for s in service_names}
    for s, compact in compacts.items():
        if compact and compact in flat:
            return s
    for s, compact in compacts.items():
        if any(len(w) >= 3 and w in compact for w in words):
            return s
    best, best_score = None, 0.0
    for s in service_names:
        target = s.lower()
        n = max(1, len(target.split()))
        grams = [" ".join(words[i:i + n]) for i in range(len(words))]
        for c in grams + words:
            if not c:
                continue
            score = difflib.SequenceMatcher(None, target, c).ratio()
            if score > best_score:
                best, best_score = s, score
    return best if best_score >= 0.65 else None


def extract_with_rules(transcript: str, service_names: list) -> dict:
    """Offline extraction: service, date, time and name from plain rules."""
    t = (transcript or "").lower()
    out = {"service": match_service(t, service_names),
           "date": None, "time": None, "name": None}

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


def try_openai_extract(transcript: str, service_names: list) -> dict | None:
    """Smarter extraction via OpenAI when a key is set, else None."""
    if not OPENAI_KEY:
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
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def free_slots(business_id: int, day: datetime, duration_min: int) -> list:
    b = get_business(business_id)
    open_h, close_h, step = b["open_hour"], b["close_hour"], b["slot_min"]
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
    business_id: int, transcript: str, caller_phone: str = "",
    call_sid: str = "", source: str = "voicemail",
) -> dict:
    """Turn a transcript into a booking. Returns {reply, appointment, parsed}."""
    b = get_business(business_id)
    svcs = services_for(business_id)
    names = [s["name"] for s in svcs]

    parsed = try_openai_extract(transcript, names) or {}
    if not any(parsed.values()):
        parsed = extract_with_rules(transcript, names)
    else:
        fb = extract_with_rules(transcript, names)
        for k in ("service", "date", "time", "name"):
            parsed.setdefault(k, fb.get(k))

    svc = next((s for s in svcs if s["name"] == parsed.get("service")),
               svcs[0] if svcs else None)
    if not svc:
        return {"reply": "Sorry, no bookable services are set up yet.",
                "appointment": None, "parsed": parsed}

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
    chosen = min(slots, key=lambda s: abs((s - want).total_seconds()), default=None)
    if not chosen:
        nxt = day + timedelta(days=1)
        slots = free_slots(business_id, nxt, svc["duration_min"])
        chosen = slots[0] if slots else None
    if not chosen:
        return {"reply": "Sorry, we're fully booked the next couple of days. "
                         "Please try calling back later.",
                "appointment": None, "parsed": parsed}

    name = parsed.get("name") or "Phone Guest"
    phone = caller_phone or ""
    m = re.search(r"(\+?\d[\d\s\-]{7,}\d)", transcript)
    if m:
        phone = re.sub(r"[\s\-]", "", m.group(1))

    ends = chosen + timedelta(minutes=svc["duration_min"])
    con = db()
    cur = con.execute(
        """INSERT INTO appointments
           (business_id, service_id, customer_name, customer_phone,
            starts_at, ends_at, status, source, created_at)
           VALUES (?,?,?,?,?,?, 'booked', ?, ?)""",
        (business_id, svc["id"], name, phone,
         chosen.isoformat(timespec="minutes"), ends.isoformat(timespec="minutes"),
         source, datetime.now().isoformat(timespec="seconds")),
    )
    appt_id = cur.lastrowid
    reply = (
        f"Got it {name}. You're booked for {svc['name']} on "
        f"{chosen.strftime('%A %B %d at %-I:%M %p')}. "
        f"We'll text {phone or 'you'} a confirmation. Thanks for calling {b['name']}!"
    )
    con.execute(
        "INSERT INTO calls (call_sid, business_id, transcript, agent_reply,"
        " appointment_id, created_at) VALUES (?,?,?,?,?,?)",
        (call_sid, business_id, transcript, reply, appt_id,
         datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    appt = con.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
    con.close()
    return {"reply": reply, "appointment": dict(appt), "parsed": parsed}


# ------------------------------------------------------ transcription ---
def _patch_av_open():
    """Compat shim: some av builds dropped the metadata_errors kwarg that
    faster-whisper passes. Strip it so decoding keeps working."""
    try:
        import av

        if getattr(av.open, "_vb_patched", False):
            return
        _orig = av.open

        def _open(*a, **k):
            k.pop("metadata_errors", None)
            return _orig(*a, **k)

        _open._vb_patched = True
        av.open = _open
    except Exception:
        pass


_LOCAL_MODEL = None


def get_local_model():
    """Load faster-whisper once and keep it in memory."""
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        _patch_av_open()
        from faster_whisper import WhisperModel

        _LOCAL_MODEL = WhisperModel(WHISPER_MODEL_SIZE, device="cpu",
                                    compute_type="int8")
    return _LOCAL_MODEL


def local_stt_name() -> str | None:
    try:
        import faster_whisper  # noqa: F401

        return f"on-device whisper ({WHISPER_MODEL_SIZE})"
    except Exception:
        return None


def transcribe_local(audio: bytes, filename: str = "voicemail.mp3") -> str | None:
    """Transcribe with on-device faster-whisper. None on failure."""
    if not audio:
        return None
    try:
        import tempfile

        suffix = "." + (filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp3")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            tmp.write(audio)
            tmp.flush()
            model = get_local_model()
            segs, _ = model.transcribe(tmp.name)
            text = " ".join(s.text.strip() for s in segs).strip()
            return text or None
    except Exception:
        return None


def transcribe_openai_api(audio: bytes, filename: str = "voicemail.mp3") -> str | None:
    """Transcribe with OpenAI Whisper API when a key is set. None otherwise."""
    if not audio or not OPENAI_KEY:
        return None
    try:
        from openai import OpenAI

        client = OpenAI()
        buf = io.BytesIO(audio)
        buf.name = filename
        resp = client.audio.transcriptions.create(model="whisper-1", file=buf)
        return (getattr(resp, "text", "") or "").strip() or None
    except Exception:
        return None


def transcribe_any(audio: bytes, filename: str = "voicemail.mp3") -> tuple[str | None, str]:
    """Best available transcription. Returns (text, engine_used)."""
    if not audio:
        return None, "none"
    text = transcribe_local(audio, filename)
    if text:
        return text, local_stt_name() or "on-device whisper"
    text = transcribe_openai_api(audio, filename)
    if text:
        return text, "openai whisper"
    return None, "none"


def fetch_recording_bytes(recording_url: str) -> tuple[bytes | None, str]:
    """Download a Twilio recording (tries plain URL, then .mp3)."""
    if not recording_url:
        return None, "voicemail.mp3"
    candidates = [recording_url]
    if not re.search(r"\.(mp3|wav)$", recording_url, re.I):
        candidates.append(recording_url.rstrip("/") + ".mp3")
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"Accept": "audio/mpeg"})
            if TWILIO_SID and TWILIO_TOKEN:
                creds = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
                req.add_header("Authorization", f"Basic {creds}")
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                if data and len(data) > 1000:
                    ext = "mp3" if "mp3" in url else "wav" if url.endswith(".wav") else "mp3"
                    return data, f"voicemail.{ext}"
        except Exception:
            continue
    return None, "voicemail.mp3"


# ------------------------------------------------- voicemail pipeline ---
def safe_filename(name: str) -> str:
    name = (name or "voicemail").strip().replace("\\", "/").split("/")[-1]
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "voicemail")[:80]


def stage_upload(business_id: int, file_bytes: bytes,
                 filename: str, caller_phone: str = "") -> dict:
    """Step 1: store the audio, mark pending. No AI runs here."""
    fname = safe_filename(filename)
    stored = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:8]}_{fname}"
    audio_path = os.path.join(UPLOAD_DIR, stored)
    with open(audio_path, "wb") as f:
        f.write(file_bytes)
    con = db()
    cur = con.execute(
        """INSERT INTO voicemails
           (business_id, from_phone, transcript, status,
            audio_path, filename, created_at)
           VALUES (?,?, '', 'pending',?,?,?)""",
        (business_id, caller_phone, audio_path, fname,
         datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    row = con.execute("SELECT * FROM voicemails WHERE id = ?", (cur.lastrowid,)).fetchone()
    con.close()
    return {"ok": True, "status": "pending", "voicemail": dict(row)}


def _audio_for_vm(vm: dict) -> tuple[bytes | None, str]:
    if vm.get("audio_path") and os.path.exists(vm["audio_path"]):
        with open(vm["audio_path"], "rb") as f:
            return f.read(), vm.get("filename") or "voicemail.mp3"
    if vm.get("recording_url"):
        return fetch_recording_bytes(vm["recording_url"])
    return None, "voicemail.mp3"


def extract_one(vm_id: int) -> dict:
    """Step 2 for a single voicemail: transcribe -> extract -> book."""
    con = db()
    row = con.execute("SELECT * FROM voicemails WHERE id = ?", (vm_id,)).fetchone()
    con.close()
    if not row:
        return {"ok": False, "error": "voicemail not found", "voicemail_id": vm_id}
    vm = dict(row)

    text = (vm.get("transcript") or "").strip()
    engine = "provided transcript"
    if not text:
        blob, fname = _audio_for_vm(vm)
        if blob:
            text, engine = transcribe_any(blob, fname) or (None, engine)
            text = text or ""

    con = db()
    if not text:
        con.execute("UPDATE voicemails SET status='failed-no-transcript' WHERE id=?",
                    (vm_id,))
        con.commit()
        con.close()
        return {"ok": False,
                "error": ("audio could not be transcribed. "
                          + ("Install: pip install faster-whisper. "
                             if not local_stt_name() else "")
                          + ("Or set OPENAI_API_KEY in backend/.env. "
                             if not OPENAI_KEY else "")),
                "transcript": "", "engine": engine, "voicemail_id": vm_id}

    result = run_booking(int(vm.get("business_id") or 1), text,
                         caller_phone=vm.get("from_phone") or "",
                         call_sid=vm.get("call_sid") or "", source="voicemail")
    status = "processed" if result.get("appointment") else "failed-no-slots"
    con.execute(
        "UPDATE voicemails SET transcript=?, status=?, appointment_id=? WHERE id=?",
        (text, status,
         result["appointment"]["id"] if result.get("appointment") else None, vm_id),
    )
    con.commit()
    updated = con.execute("SELECT * FROM voicemails WHERE id = ?", (vm_id,)).fetchone()
    con.close()
    return {"ok": True, "transcript": text, "engine": engine,
            **result, "voicemail": dict(updated), "voicemail_id": vm_id}


def extract_pending(business_id: int = 1) -> dict:
    """One AI button: run extraction over every pending voicemail."""
    con = db()
    rows = con.execute(
        "SELECT id FROM voicemails WHERE business_id = ?"
        f" AND status IN ({','.join('?' * len(PENDING_STATUSES))}) ORDER BY id ASC",
        (business_id, *PENDING_STATUSES),
    ).fetchall()
    con.close()
    results = [extract_one(r["id"]) for r in rows]
    done = sum(1 for r in results if r.get("ok") and r.get("appointment"))
    return {"ok": True, "processed": len(results), "booked": done,
            "results": results}


def ingest_twilio_voicemail(business_id: int, transcript: str, recording_url: str,
                            caller_phone: str, call_sid: str,
                            recording_sid: str, to_phone: str) -> dict:
    """Twilio <Record> callback path: transcribe (or reuse Twilio's text) + book."""
    if to_phone:
        try:
            b = business_by_phone(to_phone)
            if b:
                business_id = b["id"]
        except Exception:
            pass
    text = (transcript or "").strip()
    if not text and recording_url:
        blob, fname = fetch_recording_bytes(recording_url)
        if blob:
            text, _ = transcribe_any(blob, fname)
            text = text or ""
    con = db()
    if not text:
        cur = con.execute(
            """INSERT INTO voicemails
               (call_sid, recording_sid, business_id, from_phone, to_phone,
                recording_url, transcript, status, created_at)
               VALUES (?,?,?,?,?,?,'','failed-no-transcript',?)""",
            (call_sid, recording_sid, business_id, caller_phone, to_phone,
             recording_url, datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        vm_id = cur.lastrowid
        con.close()
        return {"ok": False, "transcript": "", "voicemail_id": vm_id,
                "reply": None, "appointment": None, "parsed": None}
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
    return {"ok": True, "transcript": text, **result, "voicemail_id": vm_id}


# ------------------------------------------------------------- twiml ---
def twiml_say_gather(say: str, action: str = "/voice/process") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{say}</Say>"
        f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto" />'
        "<Say>We didn't hear anything. Goodbye!</Say>"
        "</Response>"
    )


def twiml_record_voicemail(business_name: str,
                           action: str = "/voice/voicemail") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>Thanks for calling {business_name}. We're away right now. "
        "Please leave your name, the service you want, and what day and time "
        "works for you, after the beep. We'll text you a confirmation.</Say>"
        f'<Record action="{action}" method="POST" maxLength="120" playBeep="true" '
        'trim="trim-silence" recordingStatusCallback="/voice/voicemail" />'
        "<Say>We didn't get your message. Goodbye!</Say>"
        "</Response>"
    )


# ---------------------------------------------------------------- api ---
@app.get("/api/health")
def health():
    stt = local_stt_name() or ("openai whisper" if OPENAI_KEY else "none")
    return {
        "ok": True,
        "ai": "openai" if OPENAI_KEY else "offline-rules",
        "stt": stt,
        "twilio": bool(TWILIO_SID),
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
    svc = con.execute("SELECT * FROM services WHERE id = ?",
                      (data.get("service_id"),)).fetchone()
    dur = svc["duration_min"] if svc else 30
    start = datetime.fromisoformat(data["starts_at"])
    end = start + timedelta(minutes=dur)
    cur = con.execute(
        """INSERT INTO appointments
           (business_id, service_id, customer_name, customer_phone,
            starts_at, ends_at, status, source, created_at)
           VALUES (?,?,?,?,?,?, 'booked', 'dashboard', ?)""",
        (data.get("business_id", 1), data.get("service_id"),
         data.get("customer_name", ""), data.get("customer_phone", ""),
         start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"),
         datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    row = con.execute("SELECT * FROM appointments WHERE id = ?",
                      (cur.lastrowid,)).fetchone()
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
    return {"date": day.date().isoformat(),
            "slots": [s.isoformat(timespec="minutes") for s in slots]}


@app.post("/api/simulate-call")
async def simulate_call(req: Request):
    data = await req.json()
    return run_booking(int(data.get("business_id", 1)), data.get("transcript", ""),
                       data.get("caller_phone", ""), source="simulator")


@app.get("/api/voicemails")
def list_voicemails(business_id: int = 1):
    con = db()
    rows = con.execute(
        "SELECT v.*, a.starts_at AS appointment_starts_at,"
        " a.customer_name AS booked_name, s.name AS service_name"
        " FROM voicemails v"
        " LEFT JOIN appointments a ON a.id = v.appointment_id"
        " LEFT JOIN services s ON s.id = a.service_id"
        " WHERE v.business_id = ? ORDER BY v.id DESC LIMIT 200",
        (business_id,),
    ).fetchall()
    con.close()
    return {"voicemails": [dict(r) for r in rows]}


@app.post("/api/voicemail/upload")
async def voicemail_upload(file: UploadFile = File(...), business_id: int = 1,
                           caller_phone: str = Form(default="")):
    """Step 1: store one file as pending. AI runs in step 2."""
    audio = await file.read()
    if not audio:
        return {"ok": False, "error": "empty file"}
    return stage_upload(business_id, audio, file.filename or "voicemail.mp3",
                        caller_phone)


@app.post("/api/voicemails/upload")
async def voicemails_upload(files: list[UploadFile] = File(...), business_id: int = 1):
    """Step 1 for several files at once."""
    out = []
    for f in files:
        audio = await f.read()
        if not audio:
            out.append({"ok": False, "error": "empty file",
                        "filename": f.filename})
            continue
        out.append(stage_upload(business_id, audio,
                                f.filename or "voicemail.mp3"))
    return {"ok": True, "uploaded": len(out), "results": out}


@app.post("/api/voicemail/process")
async def voicemail_process(req: Request):
    """Direct transcript ingest (used by tests / Twilio text)."""
    data = await req.json()
    res = ingest_twilio_voicemail(
        int(data.get("business_id", 1)),
        data.get("transcript", "") or data.get("TranscriptionText", ""),
        data.get("recording_url", "") or data.get("RecordingUrl", ""),
        data.get("caller_phone", "") or data.get("From", ""),
        data.get("call_sid", "") or data.get("CallSid", ""),
        data.get("recording_sid", "") or data.get("RecordingSid", ""),
        data.get("to_phone", "") or data.get("To", ""),
    )
    return res


@app.post("/api/voicemail/{vm_id}/extract")
def voicemail_extract(vm_id: int):
    """Step 2 for one file: AI transcribe -> extract -> book."""
    return extract_one(vm_id)


@app.post("/api/voicemail/extract-all")
async def voicemail_extract_all(req: Request):
    """One AI button: process every pending voicemail."""
    try:
        data = await req.json()
    except Exception:
        data = {}
    return extract_pending(int(data.get("business_id", 1)))


@app.get("/api/voicemail/{vm_id}/audio")
def voicemail_audio(vm_id: int):
    con = db()
    row = con.execute("SELECT audio_path, filename FROM voicemails WHERE id = ?",
                      (vm_id,)).fetchone()
    con.close()
    if not row or not row["audio_path"] or not os.path.exists(row["audio_path"]):
        return PlainTextResponse("audio not found", status_code=404)
    return FileResponse(row["audio_path"],
                        filename=row["filename"] or "voicemail.mp3")


@app.delete("/api/voicemail/{vm_id}")
def voicemail_delete(vm_id: int):
    con = db()
    row = con.execute("SELECT audio_path FROM voicemails WHERE id = ?",
                      (vm_id,)).fetchone()
    if row and row["audio_path"] and os.path.exists(row["audio_path"]):
        try:
            os.remove(row["audio_path"])
        except OSError:
            pass
    con.execute("DELETE FROM voicemails WHERE id = ?", (vm_id,))
    con.commit()
    con.close()
    return {"ok": True}


# ------------------------------------------------------- twilio voice ---
@app.post("/voice/incoming")
async def voice_incoming(To: str = Form(default=""), From: str = Form(default="")):
    b = business_by_phone(To)
    return PlainTextResponse(
        twiml_say_gather(
            f"Thanks for calling {b['name']}. Tell me which service you want, "
            "and what day and time works for you. "
            "For example, say haircut tomorrow at 3 p m."),
        media_type="application/xml")


@app.post("/voice/process")
async def voice_process(SpeechResult: str = Form(default=""),
                        CallSid: str = Form(default=""),
                        To: str = Form(default=""), From: str = Form(default="")):
    b = business_by_phone(To)
    if not SpeechResult.strip():
        return PlainTextResponse(
            twiml_say_gather("Sorry, I didn't catch that. "
                             "What day and time works for you?"),
            media_type="application/xml")
    result = run_booking(b["id"], SpeechResult, caller_phone=From,
                         call_sid=CallSid, source="voice")
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{result['reply']}</Say><Hangup/></Response>",
        media_type="application/xml")


@app.post("/voice/voicemail-greeting")
async def voice_voicemail_greeting(To: str = Form(default="")):
    """Missed-call handler: point Twilio's busy/no-answer webhook here."""
    b = business_by_phone(To)
    return PlainTextResponse(twiml_record_voicemail(b["name"]),
                             media_type="application/xml")


@app.post("/voice/voicemail")
async def voice_voicemail(RecordingUrl: str = Form(default=""),
                          RecordingSid: str = Form(default=""),
                          CallSid: str = Form(default=""),
                          From: str = Form(default=""), To: str = Form(default=""),
                          TranscriptionText: str = Form(default=""),
                          TranscriptionStatus: str = Form(default="")):
    """Twilio <Record> callback: transcribe the message and book it."""
    b = business_by_phone(To)
    result = ingest_twilio_voicemail(b["id"], TranscriptionText, RecordingUrl,
                                     From, CallSid, RecordingSid, To)
    if result.get("appointment"):
        say = result["reply"]
    elif not result.get("transcript"):
        say = ("Thanks for your message. Sorry, we couldn't hear it clearly. "
               "Please call back with the service, day and time you want.")
    else:
        say = ("Thanks for your message. Sorry, we couldn't find a free slot. "
               "We'll call you back to arrange a time.")
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{say}</Say><Hangup/></Response>",
        media_type="application/xml")
