# Meeting Invite WhatsApp Template Setup

Use this for third-party attendee notifications after the bot books a Google Meet.

## Environment

Set these in `.env`:

```env
WA_MEETING_TEMPLATE_NAME=gmeet_confirmation
WA_MEETING_TEMPLATE_LANGUAGE=en_US
```

If your approved Meta template has a different internal name, use that exact name.

## Meta Template

Create or verify a Utility template in Meta WhatsApp Manager with this body:

```text
Hello, {{user_name}} has scheduled a meeting with you: {{meeting_topic}} on {{meeting_date}}. You can join using this link: {{meeting_link}}.
```

The bot sends body parameters in this exact order:

1. `user_name`
2. `meeting_topic`
3. `meeting_date`
4. `meeting_link`

## Quick Reply Buttons

Add two quick reply buttons to the same template:

1. `Add to The Calendar`
2. `NO`

The bot sends dynamic quick-reply payloads:

1. `meeting_add_calendar|<meet_link>`
2. `meeting_decline`

When the attendee taps `Add to The Calendar`, the bot replies with the Meet link.
When the attendee taps `NO`, the bot acknowledges the decline.

## Development Mode

While the WhatsApp app is in development mode, Meta only delivers template messages to test-recipient numbers. Add the attendee phone number in:

Meta Developers -> WhatsApp -> API Setup -> To / test recipient phone numbers

If Meta returns success but the attendee does not receive the message, check that:

- the attendee number is in the test-recipient list,
- the template name matches `WA_MEETING_TEMPLATE_NAME`,
- the language matches `WA_MEETING_TEMPLATE_LANGUAGE`,
- the template is approved and enabled,
- the template has exactly the body variables and quick reply buttons above.
