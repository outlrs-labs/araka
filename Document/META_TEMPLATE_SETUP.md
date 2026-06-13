# Meta / WhatsApp setup — webhook + assignee template

## A. Webhook

1. Meta App dashboard → **WhatsApp → Configuration**.
2. **Callback URL:** `https://<your-domain>/webhook`
3. **Verify token:** must equal `WA_VERIFY_TOKEN` in `.env`.
4. Click **Verify and save** (the GET handshake must return the challenge).
5. Under **Webhook fields**, subscribe to **`messages`**.

### Signature verification
Set `WA_APP_SECRET` (Meta App → Settings → Basic → *App Secret*) and keep
`REQUIRE_WA_SIGNATURE=true`. The server validates the
`X-Hub-Signature-256` header on every POST and rejects mismatches.

---

## B. Assignee notification template

When the creator books a meeting with someone, that person is contacted with
a **Meta-approved utility template** (free-form messages to people who haven't
messaged you are blocked by the 24-hour window). Reminders to the assignee use
the same template.

Create it in **WhatsApp Manager → Message templates → Create**:

- **Name:** `gmeet_confirmation` (must match `WA_MEETING_TEMPLATE_NAME`)
- **Category:** Utility
- **Language:** English (US) — must match `WA_MEETING_TEMPLATE_LANGUAGE`

**Body** (4 variables, in this order):

```
Hi! {{1}} has scheduled a meeting with you:
📌 {{2}}
🕐 {{3}}
🔗 {{4}}
```

| Variable | Maps to |
|---|---|
| `{{1}}` | Booker's name |
| `{{2}}` | Meeting title |
| `{{3}}` | Date / time |
| `{{4}}` | Google Meet link |

**Buttons** (Quick reply, in this order — the code sends these payloads):

1. `Add to calendar`  → payload `meeting_add_calendar`
2. `No` (decline)     → payload `meeting_decline`

> The bot also appends a way to opt out. When an assignee replies **STOP**,
> they are set to `OPT_OUT` and flagged unreachable; **START** re-enables.

If you change the variable order or button layout, update
`send_meeting_notification()` in `bot/services/whatsapp.py` to match.

---

## C. Test mode (before business verification)

While the app is in development:

- Add each test recipient under **WhatsApp → API Setup → recipient phone
  numbers**. Only listed numbers can receive messages.
- The bot's own number must be a registered test/sender number.
- If an assignee isn't on the test list, the booking still succeeds — the bot
  just reports it couldn't notify them.

---

## D. Going live

- Complete **Business Verification** and submit the template for review.
- Move from the temporary token to a **System User long-lived token** and put
  it in `WA_ACCESS_TOKEN`.
- Tier limits start at 250 business-initiated conversations/day — ample for
  ~100 users.
