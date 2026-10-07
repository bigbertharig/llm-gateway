#!/bin/bash
# Copy this repo to the rig and (re)start rig-gateway.service. Run from the Pi: deploy/deploy_rig.sh
set -euo pipefail
RIG="${RIG_HOST:-10.0.0.3}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
rsync -a --delete --exclude .git --exclude __pycache__ --exclude .venv --exclude '*.egg-info' --exclude results \
  "$REPO/" "$RIG:llm-gateway/"
ssh -o BatchMode=yes "$RIG" '
  set -e
  cd ~/llm-gateway
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/pip install -q -e .
  sudo -n install -m 644 ~/llm-gateway/deploy/rig-gateway.service /etc/systemd/system/
  sudo -n systemctl daemon-reload
  sudo -n systemctl enable rig-gateway.service >/dev/null
  sudo -n systemctl restart rig-gateway.service
  for i in $(seq 1 20); do curl -fsS http://127.0.0.1:8080/health 2>/dev/null && exit 0; sleep 0.5; done
  echo "rig-gateway not healthy" >&2; journalctl -u rig-gateway -n 20 --no-pager >&2; exit 1
'
