---
name: setup
description: Set up the personal local Canvas plugin without exposing credentials in chat, enable only the requested access, and check the connected account.
---

# Set up personal Canvas access

This plugin runs on the user's computer for their own account. It is not public
hosted onboarding. Institution restrictions still apply. Use the hosted OAuth
connection for a supported institutional deployment.

The plugin requires UV on PATH. The first launch obtains Python 3.12 and installs
the bundled project's pinned dependencies. Explain missing runtime or launch
errors without claiming an installation succeeded.

If the host offers plugin configuration fields, have the user enter their Canvas
HTTPS origin and their own token in those fields. Sensitive tokens belong in the
host's secure credential input. Keep the write option off unless requested.

For a host without those fields, the plugin reads an explicitly selected
CANVAS_ENV_PATH, or the documented default ~/.config/canvas-connector/canvas.env.
The private file contains CANVAS_BASE_URL, CANVAS_API_TOKEN, and optionally
CANVAS_ALLOW_WRITES=false. Help create a blank file outside repositories with
owner-only permissions when requested, then let the user fill the token locally.
Do not ask them to paste it into chat, print it, commit it, or include it in a
tool-result example. Never replace an existing configuration without checking
the user's intended account and preserving their work.

Restart or reconnect the local server after configuration. Call the connection
status tool, then the dashboard if requested, to verify which account is active.
Do not infer account access from successful plugin installation alone. Keep
writes disabled until the user requests them; enabling writes does not authorize
a particular submission or message. Follow the Canvas assistant skill for use.
