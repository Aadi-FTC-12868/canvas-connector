---
name: setup
description: Set up the personal local Canvas plugin without exposing credentials in chat, enable only the requested access, and check the connected account.
---

# Set up personal Canvas access

This plugin runs on the user's computer for their own account. It is separate
from Built by Aadi account creation and hosted onboarding. Institution
restrictions still apply. Use the current repository compatibility guide to
choose the supported local route for the user's assistant and operating system.

The account-free local guide and copyable agent prompt are at
https://builtbyaadi.com/canvas-connector/local.html. Local setup keeps the token
on the user's computer rather than in the hosted service, but security still
depends on their device and configuration. Do not promise absolute security.

If the user needs a token, explain Canvas Account → Settings → Approved
Integrations → New Access Token. This option exists only when the school enables
it; missing or disabled controls cannot be enabled by this connector. Have the
user choose an appropriate purpose and expiration and enter the token privately.

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
