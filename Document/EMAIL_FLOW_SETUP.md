# Email-from-chat (WhatsApp Flow) setup

Lets a user **send an email from WhatsApp**. They tap a button, a native form
opens (To / Subject / Body), they type it, and it's sent **from their own
Gmail**.

## The privacy guarantee (why this is built as a Flow)

The email content goes **Flow form → Gmail API**. The AI/LLM only *opens* the
form — it never writes, reads, or edits the subject or body.

```
user types in form ──nfm_reply──► handle_email_flow_completion() ──► gmail.send_email() ──► Gmail
                                   (no LLM in this path)
```

Because of this, there is **no chat fallback**: if the Flow isn't configured or
fails to open, the bot says "not available" rather than collecting the body in
chat (which would route content through the model and defeat the point).

## Scope

Sending needs the Gmail **send** scope:

```
https://www.googleapis.com/auth/gmail.send
```

It's already added to `GOOGLE_SCOPES` in `bot/config.py`. `gmail.send` is
*send-only* — it cannot read mail. Existing users will be auto-prompted to
re-authorise on their next `connect`, because `handle_connect()` detects the
new scope via `_missing_google_scopes()` and shows
*"tap to update google permissions"*.

> Note: this does not remove the need for Google app verification for
> production/external users — you already hold the restricted `gmail.readonly`
> scope. `gmail.send` is a *sensitive* scope and is incremental on top of that.

## Meta setup

1. WhatsApp Manager → **Flows** → Create flow → "Import" the JSON from
   `setup/flows/email_flow.json`.
2. Endpoint: **none** — this is a static Flow. The submission arrives as an
   interactive `nfm_reply` on the existing `/webhook` route.
3. **Publish** the Flow (requires Business Verification). While verification is
   pending, set `WA_FLOW_DRAFT_MODE=true` to test it with WABA testers.
4. Copy the published Flow ID into `.env`:
   ```
   WA_EMAIL_FLOW_ID=<your_email_flow_id>
   ```
5. Restart the bot: `sudo systemctl restart followup-bot`.

## How it works in this repo

| Piece | Location |
|---|---|
| Flow JSON (To / Subject / Body) | `setup/flows/email_flow.json` |
| Tool the AI calls to open the form | `compose_email` in `bot/tools.py` |
| Sends the Flow message | `send_email_flow()` in `bot/handlers/callback_handler.py` |
| Receives the submission + sends mail | `handle_email_flow_completion()` (same file) |
| Routes meeting-form vs email-form | `handle_flow_completion()` (same file) |
| Actual Gmail send | `send_email()` in `bot/services/gmail.py` |

The email form is told apart from the meeting form by the payload it stamps:
`intent=send_email` plus a `body` field (the meeting form has neither).

## Trying it

1. Connect Google (or re-connect to grant `gmail.send`).
2. Message the bot: **"email someone"** or **"send an email to john@x.com"**.
3. The bot opens the form. Fill in To / Subject / Body and tap **Send email**.
4. The bot replies `sent to <address>`.

## Behaviour when not set up

- `WA_EMAIL_FLOW_ID` empty → `compose_email` returns `EMAIL_FLOW_NOT_CONFIGURED`
  and the bot tells the user the feature isn't available. It will **not** draft
  the email itself.
- Google not connected / `gmail.send` missing → the bot shows a
  **Connect / Update access** button.
