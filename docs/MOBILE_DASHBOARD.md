# Mobile dashboard and Tailscale rollout

This runbook configures a restricted phone view through tailnet-only Tailscale
Serve. It assumes the gateway and LAN certificate dashboard are already working;
the phone path has its own listener, identity checks, and permissions. Start with
the [security model](REMOTE_ACCESS_SECURITY.md), then follow the staged setup
below. Keep deployment-specific identities and recovery details in private
operations notes.

Reference rollout status (2026-11-18): the 443-only policy passed validation and
the phone loaded the dashboard over cellular. Cross-device conversation
continuation and revocation still need operator verification.

## Boundaries

- `pi-lan-dashboard-app.service` remains the home-network certificate route to
  the full local dashboard.
- `pi-mobile-dashboard.service` binds `127.0.0.1:8844`. Tailscale Serve may proxy
  only this listener. It has its own `/mobile/` UI and exact `/mobile/api/...`
  operations; it never proxies a browser-supplied gateway path.
- Both views refer to the same saved gateway conversations. A turn started locally
  can be opened and continued from the phone. The mobile front door forwards a
  private server-to-server token, and the gateway marks the turn as `mobile`.
- The mobile composer accepts the same bounded Codex picture inputs and converted
  document types as the main dashboard. It previews pending attachments locally;
  pictures remain Codex-only and attachments cannot be added to an active steer.
- Mobile local-model turns use the gateway's streamed turn path. If the phone
  disconnects, the gateway continues the saved turn; the phone can reopen the
  same conversation to see progress and the final reply. Codex turns already
  run as saved background work.
- Mobile Codex turns preserve the conversation's saved access and reviewer
  settings, so continuing the same conversation does not introduce phone-only
  approval prompts. Mobile local model turns use `MOBILE_TOOL_SERVERS` rather
  than a conversation's local-control grant. The default excludes host-control
  MCP servers and gateway reset. These are independent of the local dashboard
  settings.
- The mobile UI treats the live agent status as authoritative. If a saved turn
  still says `running` after its Gateway runner disappeared, the status endpoint
  records it as interrupted and the composer returns to a normal Send action.
- Mobile HTTP permissions are separate from tool permissions. Defaults permit
  observation, conversation creation and continuation, interrupt, approval, and
  workspace reads. Rig and orchestration controls, browser file writes, and
  model-control endpoints require explicit `MOBILE_PERMISSIONS` entries.

The mobile view does not yet provide every local operation in its UI. Do not
enable privileged remote controls until an action-specific approval mechanism
and its denial tests are implemented. Ordinary Codex approvals currently use
the gateway's existing live app-server bridge; after a gateway restart a pending
approval may need reconciliation before remote reliance.

## New tailnet, first stage

1. Create a dedicated tailnet and enable device approval. Keep the operator's
   phone enrolled as a user device. Give only the Pi the `tag:pi-mobile`
   service tag. Do not tag a phone or laptop.
2. In the admin console, replace the initial allow-all policy with a reviewed
   copy of `deploy/tailscale-initial-deny.hujson.template`. Replace the example
   operator login with the real tailnet login. Save only after its policy tests
   pass. Do not paste passwords, auth keys, or recovery codes into this repo.
3. Verify the existing Wi-Fi SSH and certificate dashboard paths. Start
   `tailscaled` on the Pi, then run `sudo tailscale up --advertise-tags=tag:pi-mobile`
   from the local SSH path. The operator opens its one-time URL and signs in.
   No reusable auth key needs to be shared. Approve the Pi in the admin console.
4. Check `tailscale status`, `tailscale ip`, `tailscale debug prefs`, `ip -br a`,
   `sudo nft list ruleset`, and `sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding`.
   Confirm that Serve, Funnel, advertised routes, and exit node use are absent.
   The Pi already has IPv4 forwarding for its isolated rig network; preserve its
   `99-pi-rig-forward.conf` and the narrow `eth0` to `wlan0` firewall rule.
   Verify that no rule forwards from `tailscale0` to either local interface.
   Test that an approved tailnet device cannot yet reach the Pi's
   TCP 22, 443, 8080, or 2049 ports. Recheck LAN SSH, dashboard, rig SSH/NFS,
   and the firewall's forward-drop policy.

## Mobile listener, second stage

1. Deploy the tested gateway code and install `deploy/pi-mobile-dashboard.service`.
   Create `/etc/llm-gateway/mobile/environment` owned by root and the gateway
   service group, mode `0640`.
   It must contain `MOBILE_TAILSCALE_USER`, `MOBILE_TAILSCALE_HOST` (the Pi's
   HTTPS `*.ts.net` host), and a fresh random `MOBILE_PROXY_TOKEN` of at least
   32 characters. The same environment file is read by `pi-gateway.service`.
   Optional `MOBILE_PERMISSIONS` and `MOBILE_TOOL_SERVERS` entries are explicit
   mobile-only policy. Never log or commit the token.
2. Restart the gateway so it reads the token, then start the mobile service.
   Check `127.0.0.1:8844` is its only listener. Configure Tailscale Serve for
   HTTPS 443 to `http://127.0.0.1:8844`; inspect `tailscale serve status` before
   and after. Do not use Funnel. A Serve proxy supplies the authenticated
   `Tailscale-User-Login` header; the mobile service rejects missing or different
   identities and checks Host and Origin.
3. Replace the deny-all policy with a reviewed copy of
   `deploy/tailscale-policy.hujson.template`. Keep `acls: []` and verify no other
   grant permits Pi ingress. The operator gets only TCP 443 to the Pi tag.
   Test the policy's deny assertions and live attempts to 22, 8080, 2049, and
   rig addresses. The Pi's host forwarding policy remains drop.
4. Install Tailscale on the phone, sign in with the operator account, and approve
   the phone as a device. Test `/mobile/` over cellular with Wi-Fi off. Check a
   local conversation appears with the same ID and transcript, a phone turn is
   saved once, a local browser sees it, and a disconnect/reconnect retains work.
   Test rejected identity, wrong origin, disabled mobile permissions, and a
   phone removal from the tailnet.

Do not rely on the remote path until a test from the phone proves the actual
mobile behavior. If a Serve or policy step fails, disable Serve, restore the
previous tailnet policy, and keep the LAN front door operational.

## Further work for full remote capability

The mobile API already names rig and orchestration actions independently so
each can be granted or denied without changing the LAN dashboard. Their mobile
UI and fresh approval for high-impact actions remain to be built. Codex
conversations keep their explicitly saved access and reviewer policy when
resumed from the phone. The mobile front door still enforces its tailnet
identity, exact HTTP operations, and reduced MCP-server surface. Full remote
tool parity requires action-specific, server-enforced capability checks at each
MCP process; a broader HTML page or route allowlist alone cannot provide that
boundary.
