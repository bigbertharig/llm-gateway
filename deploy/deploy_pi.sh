#!/bin/bash
# Deploy llm-gateway. Run on the Pi from the repo: deploy/deploy_pi.sh
#  3. install and (re)start pi-rig-tunnel.service and pi-gateway.service on the Pi
set -euo pipefail
SITE_ENV="${GATEWAY_SITE_ENV:-/etc/llm-gateway/site/environment}"
test -r "$SITE_ENV" || { echo "Missing site config: $SITE_ENV" >&2; exit 1; }
# The site file is operator-owned and contains only deployment settings.
source "$SITE_ENV"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"


.venv/bin/python -c "import fastapi, uvicorn, httpx" || {
  echo "Pi .venv is missing gateway deps: .venv/bin/pip install -e '.[dev]'" >&2; exit 1; }
units=(pi-gateway.service)
if [[ "${GATEWAY_LOCAL_TUNNEL:-0}" == 1 ]]; then units+=(pi-rig-tunnel.service); fi
for unit in "${units[@]}"; do
  sudo -n install -m 644 "deploy/$unit" /etc/systemd/system/
done
if sudo -n test -s /etc/llm-gateway/lan/server.key && sudo -n test -s /etc/llm-gateway/lan/environment; then
  command -v nginx >/dev/null || { echo "Install nginx before LAN dashboard deployment" >&2; exit 1; }
  sudo -n install -m 644 deploy/pi-lan-dashboard-app.service /etc/systemd/system/
  sudo -n bash deploy/configure_lan_dashboard_nginx.sh
fi
sudo -n systemctl daemon-reload
sudo -n systemctl enable "${units[@]}" >/dev/null
sudo -n systemctl restart "${units[@]}"
if sudo -n test -s /etc/llm-gateway/lan/server.key && sudo -n test -s /etc/llm-gateway/lan/environment; then
  sudo -n systemctl enable pi-lan-dashboard-app.service nginx.service >/dev/null
  sudo -n systemctl restart pi-lan-dashboard-app.service
  if systemctl list-unit-files pi-lan-dashboard.service --no-legend 2>/dev/null | grep -q '^pi-lan-dashboard.service'; then
    sudo -n systemctl disable --now pi-lan-dashboard.service >/dev/null
  fi
  sudo -n systemctl restart nginx.service
fi
for i in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
    if [[ "${GATEWAY_LOCAL_TUNNEL:-0}" == 1 ]]; then systemctl is-active --quiet pi-rig-tunnel.service; fi
    echo "pi-gateway healthy"; exit 0
  fi
  sleep 0.5
done
echo "pi-gateway not healthy" >&2
journalctl -u pi-gateway -u pi-rig-tunnel -n 20 --no-pager >&2
exit 1
