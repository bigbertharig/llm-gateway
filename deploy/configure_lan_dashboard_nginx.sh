#!/bin/bash
# Render the dashboard TLS listener from private Pi-local settings.
set -euo pipefail
source /etc/llm-gateway/lan/environment
[[ ${LAN_DASHBOARD_BIND:-} =~ ^[0-9.]+$ ]] || exit 2
[[ ${LAN_DASHBOARD_ALLOWED_HOSTS:-} =~ ^[a-zA-Z0-9.-]+$ ]] || exit 2
repo="$(cd "$(dirname "$0")/.." && pwd)"
sed -e "s/__LAN_DASHBOARD_BIND__/${LAN_DASHBOARD_BIND}/g" \
    -e "s/__LAN_DASHBOARD_HOST__/${LAN_DASHBOARD_ALLOWED_HOSTS}/g" \
    "$repo/deploy/pi-lan-dashboard.nginx.conf.template" \
    > /etc/nginx/sites-available/pi-lan-dashboard
ln -sfn /etc/nginx/sites-available/pi-lan-dashboard /etc/nginx/sites-enabled/pi-lan-dashboard
rm -f /etc/nginx/sites-enabled/default
nginx -t
