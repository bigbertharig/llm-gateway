#!/bin/bash
# Deploy llm-gateway. Run on the Pi from the repo: deploy/deploy_pi.sh
#  1. refuse a stale remote_profiles.json (re-run tools/generate_profiles.py first)
#  2. copy the repo to the rig, where `rig start remote <id>` reads remote_profiles.json
#  3. install and (re)start pi-rig-tunnel.service and pi-gateway.service on the Pi
set -euo pipefail
RIG="${RIG_HOST:-10.0.0.3}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

python3 tools/generate_profiles.py --check

rsync -a --delete --exclude .git --exclude __pycache__ --exclude .venv --exclude '*.egg-info' --exclude results \
  "$REPO/" "$RIG:llm-gateway/"

.venv/bin/python -c "import fastapi, uvicorn, httpx" || {
  echo "Pi .venv is missing gateway deps: .venv/bin/pip install -e '.[dev]'" >&2; exit 1; }
for unit in pi-rig-tunnel.service pi-gateway.service; do
  sudo -n install -m 644 "deploy/$unit" /etc/systemd/system/
done
sudo -n systemctl daemon-reload
sudo -n systemctl enable pi-rig-tunnel.service pi-gateway.service >/dev/null
sudo -n systemctl restart pi-rig-tunnel.service
sudo -n systemctl restart pi-gateway.service
for i in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
    systemctl is-active --quiet pi-rig-tunnel.service || { echo "pi-rig-tunnel not active" >&2; exit 1; }
    echo "pi-gateway healthy"; exit 0
  fi
  sleep 0.5
done
echo "pi-gateway not healthy" >&2
journalctl -u pi-gateway -u pi-rig-tunnel -n 20 --no-pager >&2
exit 1
