# WhatsApp Flow — dynamic endpoint setup (live availability)

Your bot already uses a **static** Flow (no endpoint): when an attendee email
is missing it opens the form prefilled with name/topic/date and the first free
slot. This guide turns on the **dynamic** Flow, which adds one thing the static
one can't: the **Time list is filtered live** against your Google Calendar, and
it refreshes when you change the date inside the form.

Everything is already deployed and dormant. It switches on only after you do
the 4 steps below. Until then the static flow keeps working.

## Your URLs (VM `140.245.193.44`)
- **Flow endpoint URL:** `https://140-245-193-44.sslip.io/flow`
- Webhook (unchanged): `https://140-245-193-44.sslip.io/webhook`

## What the endpoint answers
| Action | When | Response |
|---|---|---|
| `ping` | Meta health check | `{"data":{"status":"active"}}` |
| `INIT` | form opens | SCHEDULE screen, prefilled + that day's free 04:00–12:00 slots |
| `data_exchange` `date_selected` | user picks a date | refreshed slot list for that date |
| `data_exchange` `review` | Continue tapped | SUMMARY screen with a recap |
| `complete` (client) | Schedule tapped | arrives on `/webhook` as `nfm_reply` → books the meeting |

Encryption is implemented per Meta's spec (RSA-OAEP-SHA256 for the AES key,
AES-128-GCM for the body, bit-inverted IV for the response) and verified by a
round-trip test.

---

## Step 1 — Generate the keypair on the VM
The private key must live on the server next to the app.
```bash
ssh -i ~/Downloads/<KEY_FILE> ubuntu@140.245.193.44
cd ~/followup-bot
./venv/bin/python setup/generate_flow_keys.py
```
This writes `flow_private.pem` + `flow_public.pem` in `~/followup-bot`. The app
already reads `FLOW_PRIVATE_KEY_PATH=flow_private.pem` (its working dir), so no
path change is needed. (For an encrypted key, set `FLOW_KEY_PASSPHRASE` in
`.env` and pass `--passphrase` here.)

## Step 2 — Upload the public key to Meta
```bash
./venv/bin/python setup/generate_flow_keys.py --upload      # reads .env creds
```
Expect HTTP 200. (Manual alternative: `POST https://graph.facebook.com/v21.0/<PHONE_NUMBER_ID>/whatsapp_business_encryption` with form field `business_public_key=<contents of flow_public.pem>` and the WA access token.)

## Step 3 — Point the Flow at the endpoint (Flow Builder)
1. Open Flow **Set_Gmeet1** (ID `1285391600472269`).
2. Replace the JSON with `setup/flows/gmeet_flow_dynamic.json`.
3. Bottom panel → **Endpoint** → set URL to `https://140-245-193-44.sslip.io/flow` → the health-check dot should go **green** (it calls `ping`).
4. **Publish**.

## Step 4 — Switch the bot to dynamic mode
On the VM, add to `.env` then restart:
```bash
echo 'WA_GMEET_FLOW_DYNAMIC=true' >> ~/followup-bot/.env
sudo systemctl restart followup-bot
```

## Verify
- Flow Builder Endpoint health check shows **Connected/green**.
- WhatsApp the bot: *"set a gmeet with \<someone-not-in-contacts\> tomorrow"* →
  form opens with name prefilled and **only free times** 04:00–12:00 → change
  the date → the time list refreshes → Continue → Confirm → booked + on your
  calendar, attendee notified.

## Rollback
Set `WA_GMEET_FLOW_DYNAMIC=false` (or remove it) and restart — instantly back to
the static prefilled form. Nothing else changes.

## Notes
- The same private key decrypts every request; keep `flow_private.pem` off git
  (it's rsync-excluded and not committed).
- Request integrity is guaranteed by the GCM tag + RSA key, so only your server
  can read/answer a request. (Meta also sends `X-Hub-Signature-256`; signature
  verification can be added later as defense-in-depth.)
