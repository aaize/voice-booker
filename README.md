# 📞 AI Voice Booker — Voicemail to Booking

Upload voicemail audio files. On-device AI transcribes the conversation,
pulls out service + day + time + name, and creates the booking.
**No API keys required.**

## How it works

```
1. Upload  →  dashboard dropzone  →  stored as "pending"
2. AI       →  ✨ AI Extract (one file) or ✨ AI Extract All (every file)
               faster-whisper transcribes → rules/LLM extract → slot search → booked
3. Bookings →  appear in the dashboard
```

Missed phone calls work too: Twilio records the caller, posts the audio,
and the same AI pipeline books it.

## Quickstart

```bash
cd voice-booker/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # includes faster-whisper (on-device STT)
uvicorn app:app --host 127.0.0.1 --port 8000
# serve the dashboard (new terminal):
cd ../frontend && python3 -m http.server 5502
# open http://127.0.0.1:5502
```

First AI run downloads the Whisper `base` model (~145 MB, one time).
Override with `WHISPER_MODEL=tiny|small|medium` in `backend/.env`.

## Endpoints

- `GET /api/health` — `ai` (openai/offline-rules) + `stt` engine + twilio flag
- `POST /api/voicemail/upload` — one audio file → pending
- `POST /api/voicemails/upload` — several files → pending
- `POST /api/voicemail/{id}/extract` — AI on one file
- `POST /api/voicemail/extract-all` — AI over every pending file
- `GET /api/voicemail/{id}/audio` — playback, `DELETE` — remove
- `GET /api/appointments` `DELETE /api/appointments/{id}` — bookings
- `GET /api/availability?date=...` — free slots
- `POST /api/simulate-call` — text-only test: `{"transcript": "..."}`
- `POST /voice/incoming` + `POST /voice/voicemail-greeting` + `POST /voice/voicemail` — Twilio

## Going further

- Add `OPENAI_API_KEY` to `backend/.env` for cloud Whisper fallback + smarter LLM extraction.
- `ngrok http 8000`, then set the Twilio number webhooks to `/voice/incoming`
  (answer) and `/voice/voicemail-greeting` (busy/no-answer → record → auto-book).
