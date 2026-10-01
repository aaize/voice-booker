# 📞 AI Voice Booker for Services

AI receptionist that answers calls, books appointments, and shows them on a dashboard.
Works **today with zero API keys** (offline rules), upgrades to OpenAI + Twilio when you're ready to charge.

## What you got

```
voice-booker/
  backend/app.py          # FastAPI: dashboard API + Twilio voice webhooks + booking brain
  backend/requirements.txt
  backend/.env.example
  frontend/index.html     # Dashboard: bookings, availability, call simulator
```

Key endpoints:
- `GET /api/health` — `offline-rules` vs `openai` mode
- `GET /api/appointments` `POST /api/appointments` `DELETE /api/appointments/{id}`
- `GET /api/availability?date=...` — free 30-min slots
- `POST /api/simulate-call` — test voice agent without a phone: `{"transcript": "haircut tomorrow at 3pm, I'm Alex"}`
- `POST /voice/incoming` + `POST /voice/process` — Twilio webhooks (TwiML)

## Quickstart (2 min)

```bash
cd voice-booker/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload --port 8000
# open ../frontend/index.html in browser
```

Try in dashboard → "Simulate call":
> "Hi, I want a haircut tomorrow at 3pm, my name is Alex"

## Go live with real calls (Twilio, ~15 min)

1. `ngrok http 8000`
2. Twilio Console → Phone Number → Voice webhook: `POST https://<ngrok>.ngrok.io/voice/incoming`
3. Call the number, say a service + day + time.
4. Booking appears in dashboard + `voice.db`.

Add to `.env` when ready:
```
OPENAI_API_KEY=sk-...      # better extraction than rules
TWILIO_ACCOUNT_SID=...     # for SMS confirmations (next step)
TWILIO_AUTH_TOKEN=...
```

## SaaS roadmap (to first $)

1. **Week 1:** multi-business auth, Google Calendar sync (`backend/app.py: calendar placeholder`), SMS confirm via Twilio.
2. **Week 2:** Stripe: $99/mo + $0.15/min. Add `businesses.stripe_id`, gate `/voice/*` on subscription.
3. **Advanced (your goal):** swap `free_slots()` to Postgres + pgvector, realtime voice with LiveKit/Twilio Media Streams, eval harness on `calls` table (booking accuracy %).

Sell it locally: dentists, barbers, cleaners. Pitch: "never miss a booking — $99/mo, free 7-day trial on your own number."

## Upgrade hints

- `run_booking()` is the whole agent — LLM extraction (`try_openai_extract`) → slot search (`free_slots`) → SQLite write. Replace rules with LangGraph here.
- `calls` table = training data for evals. Log every transcript + outcome.
