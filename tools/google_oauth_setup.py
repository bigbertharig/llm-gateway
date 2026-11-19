#!/usr/bin/env python3
"""Authorize the dedicated rig Google account through a laptop SSH tunnel.

Run on the Pi after tunneling laptop port 8765 to Pi port 8765. OAuth data is
saved only on the Pi; never paste the resulting URL or token into chat logs.
"""

import os
import tempfile
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/drive",
          "https://www.googleapis.com/auth/documents",
          "https://www.googleapis.com/auth/spreadsheets"]


def main():
    directory = Path(os.environ.get("GOOGLE_WORKSPACE_CONFIG_DIR") or
                     (Path.home() / ".config/llm-gateway/google")).resolve()
    secret = directory / "client_secret.json"
    token = directory / "token.json"
    expected = os.environ.get("GOOGLE_WORKSPACE_ACCOUNT", "bigbertharig@gmail.com").lower()
    if not secret.is_file() or secret.is_symlink():
        raise SystemExit(f"Install the OAuth desktop client at {secret} first")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(host="localhost", port=8765, open_browser=False,
                                  authorization_prompt_message="Open this URL on your laptop:\n{url}\n")
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    actual = gmail.users().getProfile(userId="me").execute().get("emailAddress", "").lower()
    if actual != expected:
        raise SystemExit("Wrong Google account authorized; token was not saved")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                     prefix=".google-token-", delete=False) as tmp:
        tmp.write(creds.to_json())
        tmp.flush()
        os.fchmod(tmp.fileno(), 0o600)
        staged = Path(tmp.name)
    os.replace(staged, token)
    print(f"Authorized {actual}; private token saved at {token}")


if __name__ == "__main__":
    main()
