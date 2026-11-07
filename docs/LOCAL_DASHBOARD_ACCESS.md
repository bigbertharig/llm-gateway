# Direct local dashboard access

The browser entry point is `https://raspberrypi.local/gpurig` on the home Wi-Fi. The desktop already resolves that mDNS name. This phase does not expose the rig or make the dashboard reachable from outside the home network.

Nginx binds HTTPS only to the Pi Wi-Fi address and requires a client TLS certificate signed by the Pi's private dashboard CA. It advertises that CA in its TLS certificate request so browsers show only matching client certificates. It proxies only the dashboard page and its explicit dashboard API routes. The gateway itself remains on `127.0.0.1:8080`; the manager, inference, and orchestration APIs are not LAN routes. POST actions require a matching browser Origin, `Sec-Fetch-Site: same-origin` when present, and JSON. A device without an enrolled certificate receives no dashboard content.

An SSH key cannot authenticate an ordinary browser request, so the desktop has its own browser client certificate. The private client key is held in the current user's certificate store and is not reused as an SSH key. The TLS request advertises the Pi dashboard CA, so Chrome should offer only its matching desktop certificate. The operator can still use the original SSH tunnel as a fallback, but no launcher or tunnel is required for the direct URL.

## Deployment

The loopback app unit is `deploy/pi-lan-dashboard-app.service`. `deploy/configure_lan_dashboard_nginx.sh` renders `deploy/pi-lan-dashboard.nginx.conf.template` from Pi-local settings; `deploy/deploy_pi.sh` starts both only after private certificate and environment files exist. Nginx must be installed. One-time setup uses `deploy/setup_lan_dashboard_certs.sh <pi-mdns-name> <pi-wifi-ip>` as root. The setup script refuses to overwrite an existing CA. The real Wi-Fi address and allowed browser host live in `/etc/llm-gateway/lan/environment` on the Pi, outside the public repo. Certificate private keys and enrollment bundles also stay outside the repo. The CA private key is root-readable only; the server key is readable by the gateway service group.

To enroll an approved Windows desktop, transfer `ca.crt`, `desktop.p12`, and its password file from the Pi's owner-only enrollment directory over the existing SSH connection. Import the CA into `Cert:\CurrentUser\Root` and the P12 into `Cert:\CurrentUser\My`, then delete all transferred enrollment files and the Pi's temporary enrollment bundle. Never commit or email these files. A new device gets its own client certificate; do not copy the desktop's P12 to it.

The current single-CA setup does not provide per-device certificate revocation. If a device is lost or its key is compromised, stop the LAN service, rotate the CA and server/client certificates, remove the old CA trust from approved devices, then re-enroll. Add explicit per-device revocation before enrolling more devices. The browser UI still has powerful rig and plan controls; only trusted operator devices should receive certificates.

## Verification

- With the enrolled desktop certificate, `https://raspberrypi.local/gpurig` returns the dashboard and its API calls work.
- Without a client certificate, Nginx rejects the request and serves no dashboard content.
- With a certificate, a direct `/v1/manager/status` request to the LAN listener returns 404. Cross-origin POSTs return 403.
- `ss -ltn` on the Pi shows the main gateway on `127.0.0.1:8080`, the route proxy on `127.0.0.1:8843`, and Nginx HTTPS on the Wi-Fi address only. No port 80 listener is enabled.
- The browser address uses HTTPS. There is no port 80 redirect or public router port forward.

Verified 2026-11-06: the enrolled desktop received HTTP 200 and dashboard HTML at the direct URL; a request without a client certificate was rejected; a client-certificate request to `/v1/manager/status` returned 404. The gateway and LAN services were active, eight gateway/dashboard tests passed, and the listeners were limited to Pi loopback for port 8080 and Pi Wi-Fi for port 443. Browser-origin rejection was tested with a mock upstream. Automatic approval review rejected a proposed live POST against a rig-kill endpoint because it could disrupt GPU workloads, so no live control action was used for verification.

Verified after the TLS front-end change: OpenSSL reports `Acceptable client certificate CA names: CN=Pi Dashboard Local CA`; desktop certificate request returned HTTP 200, a request without a client certificate was rejected with HTTP 400, and the manager route remained 404. The operator confirmed Chrome now offers one certificate in Incognito, with a selection prompt remaining.

## Chrome certificate chooser

Advertising the Pi CA narrows Chrome's chooser to the enrolled client certificate; Incognito can still show a one-choice prompt because it starts a fresh browser session. Chrome's `AutoSelectCertificateForUrls` policy can remove this prompt for the exact dashboard origin. Use one list entry with pattern `https://raspberrypi.local` and filter `ISSUER.CN=Pi Dashboard Local CA`, `SUBJECT.CN=bryan-desktop`. Keep that rule on the desktop only; do not add it to the public repo. Verify its status in `chrome://policy` and test in a new Incognito window. This policy sends the matching client certificate automatically to that origin.

The operator's Windows registry denied the agent's write to `HKCU\Software\Policies\Google\Chrome`, so this rule is not installed. The server-side CA hint and certificate verification are active independently of the optional browser policy.

## Chat access

The LAN proxy allows only the Chat tab's explicit options, conversation, and turn routes. All mutation routes use the same same-origin and JSON checks as rig controls. Chat history stays on the Pi's 4 TB volume; model inference remains behind the loopback gateway and rig manager. The LAN proxy and Nginx read timeouts are long enough for a cold profile load plus generation.
