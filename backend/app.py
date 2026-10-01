"""Voice Booker backend — FastAPI + SQLite + Twilio voice webhooks.

Run:
  cd backend
  python -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  cp .env.example .env   # then add OPENAI_API_KEY / Twilio keys
  uvicorn app:app --reload --port 8000

Dashboard: open ../frontend/index.html (calls http://127.0.0.1:8000)
Twilio test without a phone: POST /api/simulate-call {"transcript": "..."}
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, os.environ.get("DATABASE", "voice.db"))

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
