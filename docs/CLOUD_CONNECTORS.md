# Cloud account connectors for dashboard chat

## Google Workspace

The dedicated account is `bigbertharig@gmail.com`. Gateway owns OAuth. Harness provides a read-only MCP surface to all Chat models through the shared tool catalog. No Google credential is sent to a model or stored in Harness receipts. The old rig helper's token is expired and is not used by this connector.

The dedicated Pi account completed OAuth on 2026-11-18. Gateway's private service environment enables the connector. Gmail and Drive account checks succeeded without reading message or file content, and Gateway health passed after restart. The private token and service environment are deliberately outside Git.

The OAuth desktop client secret is installed at `~/.config/llm-gateway/google/client_secret.json` with mode `0600`. The new token belongs at `~/.config/llm-gateway/google/token.json`, also mode `0600`. Google dependencies are declared in the `google` optional dependency group and installed in the Pi Gateway venv.

To authorize from a laptop, open an SSH session with local forwarding, then run the setup script inside that session:

```sh
ssh -L 8765:127.0.0.1:8765 bryan@pi-wifi
/home/bryan/llm-gateway/.venv/bin/python /home/bryan/llm-gateway/tools/google_oauth_setup.py
```

Open the printed localhost consent URL in the laptop browser. Choose only the dedicated account. The script verifies the Gmail profile address before saving the token. Keep the consent URL and token out of chat logs.

After authorization, configure the Gateway service with `GOOGLE_WORKSPACE_ENABLED=1`, `GOOGLE_WORKSPACE_ACCOUNT=bigbertharig@gmail.com`, and `GOOGLE_WORKSPACE_TOKEN_FILE=/home/bryan/.config/llm-gateway/google/token.json`, then restart Gateway. The connector fails closed on a missing token, broad file permissions, or an account mismatch. Its four endpoints are read-only and bound by the existing Gateway authentication middleware.

Current limitation: Gateway and Harness still run as the same Unix user. The model-facing tool interface does not expose the token, but a true OS credential boundary requires moving the Google connector to a separate service identity with a narrow local IPC channel. Do that before expanding Coding mode or adding write operations.

## Claude

The dashboard's Claude option uses the Anthropic Messages API. It is configured only with a private API key file and an explicit model ID (`CLAUDE_API_KEY_FILE` and `CLAUDE_API_MODEL`). It appears in the model picker only when both are present; it never consumes rig GPUs. Chat and Coding turns use the same reviewed Harness MCP catalog and mode-specific tool grants as local models. The initial implementation supports text and extracted document attachments. API response text currently appears when the answer completes; incremental streaming and image input remain to be added.

The Pi's existing Claude Code CLI login is separate. A Claude.ai subscription session is not used as a dashboard API credential. Create an API key in the Anthropic Console, provision it in a Pi-only file with mode `0600`, configure the two environment variables, and restart Gateway. Do not paste the key into chat or commit it. The Gateway never returns the key to the browser.

As with the Google token, a same-user Pi process with unrestricted filesystem access can still read this file. Moving credentialed connectors to a separate service identity is required for a stronger OS boundary.

The Claude Messages path records a saved dashboard conversation, usage counts, bounded tool activity, and failures. Tool dispatch reuses Gateway's `tool_runtime`, which gets schemas from the Harness-authored catalog. A configured Claude model must pass a live account and tool round-trip before this path is considered production ready.
