# Cloud account connectors for dashboard chat

## Desktop Cloud settings

The desktop dashboard's **Cloud** tab owns direct API-provider configuration. It stores provider
keys under `~/.config/llm-gateway/cloud/` by default, with a private directory and `0600` key files.
The browser may replace or remove a key, but Gateway never returns a stored key. Set
`CLOUD_SETTINGS_ROOT` only when the service needs a different private location.

OpenRouter model discovery reads the provider's live model catalog after a key is saved. The
operator selects the models Gateway publishes; unselected models remain unavailable in dashboard
chat and the OpenAI-compatible `/v1/models` route. Published IDs use
`openrouter/<provider>/<model>`. OpenRouter conversations use the reviewed Harness tool catalog and
do not consume rig GPUs. The initial provider path supports text and extracted documents through
Chat Completions. Native image input and the Responses API remain outside this contract.

Cloud configuration is available only through the certificate-protected desktop dashboard. The
mobile dashboard has no credential-management routes or forms.

## Google Workspace

Configure a dedicated Google account for the rig; the account address belongs in private service configuration. Gateway owns OAuth. Harness provides one model-independent MCP surface to all Chat models and eligible delegated children through the shared tool catalog. Qwen, Nemotron, small local models, Codex, and configured API providers receive the same schemas; model routing never owns Google access. No Google credential is sent to a model or stored in Harness receipts. The old rig helper's token is expired and is not used by this connector.

The current deployment completed OAuth on 2026-11-18. Gateway's private service environment enables the connector. Gmail and Drive account checks succeeded without reading message or file content, and Gateway health passed after restart. The private token and service environment are deliberately outside Git.

The OAuth desktop client secret is installed at `~/.config/llm-gateway/google/client_secret.json` with mode `0600`. The new token belongs at `~/.config/llm-gateway/google/token.json`, also mode `0600`. Google dependencies are declared in the `google` optional dependency group and installed in the Pi Gateway venv.

To authorize from a laptop, open an SSH session with local forwarding, then run the setup script inside that session:

```sh
ssh -L 8765:127.0.0.1:8765 <pi-user>@<pi-host>
cd <gateway-checkout>
.venv/bin/python tools/google_oauth_setup.py
```

Open the printed localhost consent URL in the laptop browser. Choose only the dedicated account. The script verifies the Gmail profile address before saving the token. Keep the consent URL and token out of chat logs.

The OAuth grant includes `gmail.readonly`, `gmail.send`, `drive`, `documents`, and `spreadsheets`. Expanding an existing read-only token requires running the setup script again and accepting the new grant. After authorization, configure the Gateway service with `GOOGLE_WORKSPACE_ENABLED=1`, `GOOGLE_WORKSPACE_ACCOUNT=<dedicated-google-account>`, `GOOGLE_WORKSPACE_TOKEN_FILE=/home/<pi-user>/.config/llm-gateway/google/token.json`, and optionally `GOOGLE_WORKSPACE_RECEIPT_DIR`. Set `GOOGLE_WORKSPACE_UPLOAD_ROOTS` to one or more absolute directories, separated by the OS path separator, to permit image exports from those directories. Then restart Gateway. The connector fails closed on a missing token, broad file permissions, an account mismatch, or missing scopes.

The surface supports bounded Gmail search/read/send; Drive search/read-text/create-folder/upload-text/upload-image; Docs create/append; and Sheets create/read/update/append. Image upload accepts PNG, JPEG, and WebP files up to 64 MiB from configured export roots. Gateway validates the local path before opening it and keeps the OAuth token. The surface has no delete, trash, sharing, permission, arbitrary file upload, or mailbox-modification operation. Every mutation requires `confirmed=true` and a caller-generated `request_id`. Gateway writes a private `pending` receipt before the external call and a `completed` receipt afterward. Reusing a completed request ID returns the original receipt without repeating the change; a pending receipt after interruption is treated as an uncertain outcome and must be inspected instead of retried under a new ID.

Run the focused Gateway contracts with `.venv/bin/python tools/run_test_suite.py google_workspace`. The suite manifest is `tests/suites.json`; it excludes unrelated manager, UI, and provider tests. Harness has a separately owned suite of the same name for its MCP and delegated-child boundary.

Current limitation: Gateway and Harness still run as the same Unix user. The model-facing tool interface does not expose the token, delegated Google children receive no shell or filesystem tools, and Coding mode does not receive the Google surface. A separate service identity with narrow local IPC remains the stronger production boundary. The dedicated account contains only rig data, which bounds the current personal deployment's account-level blast radius.

## Claude and Codex authentication

All interactive credential management and setup guidance lives in the desktop **Cloud** tab, not
the Chat or Coding sidebars. Claude exposes two separate model surfaces because its subscription
and API products have different credentials:

- **Claude (API)** uses the Anthropic Messages API, the stored API key, and an explicit model ID.
- **Claude Code (subscription)** uses the gateway host's Claude Code login and a persistent CLI
  session. The Cloud tab relays Claude Code's browser authorization URL and one-time-code prompt,
  so sign-in and sign-out are managed without moving back to the Chat sidebar or an SSH terminal.
  Dashboard sessions start Claude Code in safe, restricted mode rooted at the selected workspace.

Existing deployments using `CLAUDE_API_KEY_FILE` and `CLAUDE_API_MODEL` continue to work as a
migration fallback until the Cloud tab stores a replacement. Neither Claude path consumes rig GPUs.
Chat and Coding turns use the same reviewed Harness MCP catalog and mode-specific tool grants as
local models. The initial implementation supports text and extracted document attachments. API
response text currently appears when the answer completes; incremental streaming and image input
remain to be added.

A Claude.ai subscription session is never treated as an Anthropic API credential. The two paths
remain independent, so both can be configured at once.

Codex exposes independent **Codex (Subscription)** and **Codex (API)** model entries. The Cloud tab
starts the ChatGPT device flow for the subscription instance and hands a pasted OpenAI key to a
second Codex app-server instance with an isolated private `CODEX_HOME`. Either or both can be
configured, and signing out of one does not affect the other. The Gateway never returns a stored
provider key to the browser.

As with the Google token, a same-user Pi process with unrestricted filesystem access can still read this file. Moving credentialed connectors to a separate service identity is required for a stronger OS boundary.

The Claude Messages path records a saved dashboard conversation, usage counts, bounded tool activity, and failures. Tool dispatch reuses Gateway's `tool_runtime`, which gets schemas from the Harness-authored catalog. A configured Claude model must pass a live account and tool round-trip before this path is considered production ready.
