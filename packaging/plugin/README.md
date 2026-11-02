# Canvas Connector personal local plugins

This download contains the connector source and assistant skills, with local
marketplace catalogs for Codex and Claude Code. It is for your own Canvas account,
not the public hosted OAuth onboarding flow. It does not connect a web/mobile
assistant or publish a listing in either provider's public directory.

Install [UV](https://docs.astral.sh/uv/getting-started/installation/) first. UV
obtains Python 3.12 and the pinned dependencies on the first launch. Keep this
extracted directory available while using its local marketplace.

## Codex

Run these from the extracted directory:

```sh
codex plugin marketplace add .
codex plugin add canvas-connector-personal@built-by-aadi-local
```

Restart your local session, then run the plugin's setup skill. For desktop-only
installation, add the local marketplace before using its install link. A public
website cannot register a local marketplace through a universal install URL.

Codex personal configuration uses a private file at
`~/.config/canvas-connector/canvas.env`, or `CANVAS_ENV_PATH` if explicitly set in
the launching environment. Keep it outside repositories, readable only by you.
Enter `CANVAS_BASE_URL` and `CANVAS_API_TOKEN` locally; never paste a token into
chat. Writes default to off. Add `CANVAS_ALLOW_WRITES=true` only if you intend to
enable the prepared action tools; exact actions still need your authorization.

## Claude Code

Run these from the extracted directory:

```sh
claude plugin marketplace add .
claude plugin install canvas-connector-personal@built-by-aadi-local
```

Configure the plugin's Canvas address, personal token, and write option when the
host prompts. The token field is marked sensitive. Restart or reload the plugin
as your Claude Code version requires, then ask it to check the connection.
Older clients without plugin configuration support need the source/stdio setup
instead; a valid manifest alone does not prove runtime compatibility.

For Claude Desktop chat, use the separate `.mcpb` extension download. It has its
own installation flow and does not automatically install these plugin skills.

## Check access

Check the connected account before working with course data. A successful
download or plugin registration does not prove Canvas access. Your school may
restrict API access. Do not use another person's token or share this configured
installation. Refer to the included license and security documentation.

Official setup references: [Codex plugins](https://developers.openai.com/plugins/build/plugins),
[Claude Code plugins](https://code.claude.com/docs/en/discover-plugins),
[Canvas OAuth](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth).
