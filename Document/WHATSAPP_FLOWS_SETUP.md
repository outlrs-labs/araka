# WhatsApp Flows setup for this bot

## Oracle Endpoint URLs

Your Oracle Cloud deployment uses Caddy + sslip.io:
- Public base URL: `https://140-245-193-44.sslip.io`


- WhatsApp webhook callback: `https://129-159-235-161.sslip.io/webhook`
- Webhook verify token: `WA_VERIFY_TOKEN` from `.env`
- Health check: `https://129-159-235-161.sslip.io/health`
- Google OAuth callback: not required by default. This repo currently uses the
  localhost paste flow, so keep `BASE_URL=` blank on the Oracle VM. Only use
  `https://129-159-235-161.sslip.io/auth/callback` if you later change
  `bot/services/google_auth.py` to use `BASE_URL`.

For the current static meeting Flow in `setup/flows/gmeet_flow.json`, there is
no separate Flow data endpoint. The finished Flow arrives as an interactive
`nfm_reply` message on the same `/webhook` route.

If you switch to `setup/flows/gmeet_flow_dynamic.json`, add a dedicated HTTPS
data-exchange endpoint first, for example
`https://129-159-235-161.sslip.io/flow-endpoint`, then configure that endpoint
in Meta's Flow builder/API and implement encrypted Flow Data API request
handling on the server.

## Meta setup

1. Meta App Dashboard -> WhatsApp -> Configuration.
2. Set Callback URL to `https://129-159-235-161.sslip.io/webhook`.
3. Set Verify token to the exact `WA_VERIFY_TOKEN` in `.env`.
4. Subscribe to the `messages` webhook field.
5. Keep `WA_APP_SECRET` set and `REQUIRE_WA_SIGNATURE=true`.
6. In WhatsApp Manager -> Flows, create/import the Flow JSON from
   `setup/flows/gmeet_flow.json`.
7. Publish the Flow.
8. Copy the published Flow ID into `.env` as `WA_GMEET_FLOW_ID`.
9. Restart the bot.

## Oracle VM setup checks

1. SSH into the VM:
   `ssh -i ~/Downloads/<KEY_FILE> ubuntu@129.159.235.161`
2. Check the bot service:
   `systemctl status followup-bot --no-pager`
3. Check local app health on the VM:
   `curl http://127.0.0.1:5000/health`
4. Check public HTTPS health from your Mac/browser:
   `https://129-159-235-161.sslip.io/health`
5. After changing `.env` on the VM, restart:
   `sudo systemctl restart followup-bot`

## Sending and receiving in this repo

- Sending happens in `bot/services/whatsapp.py` via `send_flow()`.
- The meeting form is opened from `send_gmeet_flow()` in
  `bot/handlers/callback_handler.py`.
- Completion is received on `/webhook` as `interactive.nfm_reply.response_json`
  and handled by `handle_gmeet_flow_completion()`.

## Google/Gmail setup

The bot now requests Gmail readonly permission. Existing connected users should
send `connect` again and approve the updated Google permission. If Google says
the app is still connected but Gmail fails, send `disconnect`, then `connect`.
