# Source provenance

This repository is a separate source release of Aadi Chandraya's Canvas connector. It was prepared in response to the owner's explicit request to make the connector open source. Project-authored code in this release is distributed under the repository's [MIT license](../LICENSE).

## Included source

| Location | Origin |
| --- | --- |
| `packages/canvas-py/` | Allowlisted Python transport, endpoint, model, navigation, document-reader, and test source from the maintained local Canvas library. |
| `packages/canvas-mcp-v2/` | Allowlisted Python MCP adapter and tests from the maintained local connector. This is a compatibility dependency; its older public tool surface is not the new application's tool contract. |
| `src/canvas_connector/` | New request-scoped adapter, response normalization, prepared actions, hosted OAuth, encrypted storage, network guard, CLI, and setup interface created for this release. |
| `docs/`, `skills/`, `tests/`, deployment files | Release documentation, a companion assistant skill, integration tests, and packaging created for this release. |

The source copy excludes private credentials, school content, course exports, account caches, local machine registrations, archived experiments, and vendored deployment trees. The canonical private workspace is not the publication root. Release scans and packaging checks belong in [VALIDATION.md](VALIDATION.md).

No Instructure mobile-app reference tree or other third-party reference implementation was copied into this release. Public documentation was consulted for API contracts and client interoperability; citations appear in the relevant guides. This is a statement about this release's source-selection process, not a legal opinion about all historical work.

Installed dependencies retain their respective licenses. The project's MIT license does not relicense them, Canvas itself, provider trademarks, or any student/instructor materials retrieved by the connector. Distribution through an AI provider's directory also has that provider's separate review and account requirements.
