# Reminder template setup (fixes the 24-hour-window problem)

## Why this exists

A reminder is a **business-initiated** message. WhatsApp only allows **free-form**
business messages within **24 hours of the user's last message**. If a user sets
a reminder and then goes quiet for more than a day (very common for birthday /
one-off reminders), the reminder fires but WhatsApp **rejects** the free-form
send (error `131047`) — so it silently never arrives.

The fix: when the free-form send is rejected, the bot falls back to an **approved
utility template**, which WhatsApp allows **anytime**. The code already does this
(`reminder_scheduler._fire_reminder_async` → `send_reminder_notification`). You
just need to create the template and set its name in `.env`.

## 1. Create the template in Meta

WhatsApp Manager → **Account tools → Message templates → Create template**

| Field | Value |
|---|---|
| **Name** | `reminder_notification` |
| **Category** | **Utility** *(not Marketing — utility is cheaper + correct)* |
| **Language** | English (US) — `en_US` |

**Body** (one variable `{{1}}`):

```
Reminder from araka

{{1}}

Reply here anytime to add, edit, or cancel your reminders.
```

When Meta asks for a **sample** for `{{1}}`, use something like: `Krishna's birthday`

> Keep the static text — Meta rejects templates whose body is *only* a variable.
> No header/footer/buttons are needed.

## 2. Wire it up

Once **approved**, set in `.env` (VM and local):

```
WA_REMINDER_TEMPLATE_NAME=reminder_notification
WA_REMINDER_TEMPLATE_LANGUAGE=en_US
```

Restart: `sudo systemctl restart followup-bot`

## 3. How it behaves

```
reminder fires
   │
   ├─ user messaged within 24h?  → free-form "reminder: ..."  (unchanged)
   │
   └─ user quiet >24h?           → free-form rejected (131047)
                                   → send reminder_notification template  ✅ delivered
                                   → if template also fails → roll back, retry in 5 min
```

- **If the template isn't configured** (`WA_REMINDER_TEMPLATE_NAME` empty), behaviour
  is unchanged — out-of-window reminders still fail. So this only helps once the
  template is approved and set.
- Templates cost a fraction of a rupee each (utility category); only out-of-window
  reminders use one, so volume is low.

## 4. Verify

Set a reminder, then don't message the bot for 24h+ (or test with a number that
hasn't messaged recently). When it fires, check the logs:

```bash
journalctl -u followup-bot | grep -i "delivered via template"
```

You should see `Reminder #N delivered via template (out-of-window)`.
