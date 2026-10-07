#!/bin/bash
# Copy this repo to the rig and (re)start rig-gateway.service. Run from the Pi: deploy/deploy_rig.sh
set -euo pipefail
RIG="${RIG_HOST:-10.0.0.3}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
rsync -a --delete --exclude .git --exclude __pycache__ --exclude .venv --exclude '*.egg-info' \
  "$REPO/" "$RIG:llm-gateway/"
ssh -o BatchMode=yes "$RIG" '
  set -e
  sudo -n install -m 644 ~/llm-gateway/deploy/rig-gateway.service /etc/systemd/system/
  sudo -n systemctl daemon-reload
  sudo -n systemctl enable rig-gateway.service >/dev/null
  sudo -n systemctl restart rig-gateway.service
  sleep 1
  systemctl is-active rig-gateway.service
  curl -fsS http://127.0.0.1:8080/health
'
