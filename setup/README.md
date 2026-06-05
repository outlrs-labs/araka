# FollowUp Bot — Setup (v1, ~100 users)

A WhatsApp-native commitment assistant: schedule meetings, loop in the other
person, remind both sides, follow up. This guide gets it running locally and
points at production notes for a small (≤100-user) deployment.

> **Scale note.** At 100 users this runs comfortably as a *single process*:
> SQLite (WAL) for storage and APScheduler for timers. No AWS/Postgres/SQS
> required. Those only become necessary when you outgrow one box.

---

## 1. Prerequisites

- **Python 3.10+** (3.11 recommended; 3.9 works but is end-of-life).
- A **Meta / WhatsApp Business** app with Cloud API access.
- A **Groq** API key (LLM).
- A **Google Cloud** OAuth client (`credentials.json`, *Desktop* type).
- **ngrok** (or any HTTPS tunnel) for local webhook + OAuth callback.

---

## 2. Install

```bash
git clone <your-repo>
cd "Bot tele copy"
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Configure

```bash
cp setup/.env.example .env
python setup/generate_secrets.py     # prints verify token, Fernet key, Flask secret
```

Paste the generated values into `.env`, then fill in the Meta + Groq values.

Put your Google OAuth client at the project root as **`credentials.json`**.

**Security must-dos (Week 1):**
- Set `GOOGLE_TOKEN_ENCRYPTION_KEY` — without it, Google tokens are stored in
  plaintext.
- Set `WA_APP_SECRET` and keep `REQUIRE_WA_SIGNATURE=true` so unsigned
  webhooks are rejected.
- Rotate any credential that was ever committed or shared.

---

## 4. Run

```bash
./setup/run_local.sh
# in another terminal:
ngrok http 5000
```

Copy the ngrok HTTPS URL into `.env` as `BASE_URL`, then restart the bot.

Quick checks:
- `BASE_URL/` → landing page
- `BASE_URL/health` → `{"status":"ok"}`
- `BASE_URL/privacy` → privacy notice (linked during onboarding)

---

## 5. Connect the Meta webhook

In the Meta App dashboard → **WhatsApp → Configuration → Webhook**:

- **Callback URL:** `https://<your-ngrok>/webhook`
- **Verify token:** the `WA_VERIFY_TOKEN` from your `.env`
- **Subscribe** to the `messages` field.

See [`META_TEMPLATE_SETUP.md`](./META_TEMPLATE_SETUP.md) for the assignee
notification template and test-number setup.

---

## 6. First run / onboarding

Message the bot from WhatsApp. A brand-new number is walked through FR-1:

1. Confirm your WhatsApp number
2. Tell it your name
3. Confirm timezone (IST default)
4. Optionally connect Google

After that, try:

```
Meet Priya tomorrow 4 PM
Remind me to call the bank in 10 minutes
Add buy milk, pay rent to my list
```

---

## 7. Test it like a user

The [`tester/`](../tester/) folder simulates the PRD Appendix-A scripts
(onboarding, happy path, conflict, missing info, reschedule) against the real
handlers with a mocked WhatsApp transport — no live Meta/Groq needed.

```bash
python -m tester.run        # or: ./tester/run_tests.sh
```

---

## 8. Production notes (still single-box)

- Run under a process manager (systemd / pm2 / supervisor) so it restarts on
  crash. In-flight OAuth and pending reminders are persisted, so a restart is
  safe.
- Put it behind a stable HTTPS domain (not ngrok) for real users.
- Back up `notebot.db` (and the `-wal`/`-shm` files) regularly.
- Watch logs for `WA API error` (Meta rate/window issues) and reminder
  retries.

## 9. What's implemented (PRD P0)

| Area | Status |
|---|---|
| FR-1 onboarding (WA#, name, tz, Google) | ✅ |
| FR-2/7 every meeting becomes a tracked Task + confirmation gate | ✅ |
| FR-3 per-field confidence | ✅ |
| FR-5 conflict detection (creator + assignee) | ✅ |
| FR-8 assignee onboarding, E.164, template-only first contact, STOP | ✅ |
| FR-9 reminders to both parties, 3× retry w/ backoff | ✅ |
| Webhook signature enforcement | ✅ (set the secret) |
| Encrypted Google tokens at rest | ✅ (set the key) |
| Durable OAuth state + reminders across restart | ✅ |
