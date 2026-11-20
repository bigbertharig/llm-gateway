# Mobile access security model

This guide describes the trust boundaries and reusable security rules for remote phone access. Keep deployment-specific identities, device IDs, addresses, certificates, and recovery details in private operations notes. Follow the [rollout runbook](MOBILE_DASHBOARD.md) to configure the phone path; see [local dashboard access](LOCAL_DASHBOARD_ACCESS.md) for the separate LAN path and the synthetic [443 grant](../deploy/tailscale-policy.hujson.template) policy. Begin with an empty grant and ACL list, as described below.

## Traffic and authority

```text
approved phone -- Tailscale identity + 443 grant --> Pi Tailscale Serve (HTTPS)
  --> loopback mobile dashboard (exact user, host, origin, route and permission checks)
  --> loopback gateway (private service token, mobile request context)
  --> saved conversations / admitted tools / isolated rig manager

trusted LAN browser -- client certificate --> separate LAN proxy --> full local dashboard
```

Only the Pi joins the tailnet as a service node. The phone joins as an approved user device. The GPU host and worker subnet stay off the tailnet. Serve makes the mobile listener available *inside* the tailnet; Funnel, router port forwarding, exit-node use, subnet routing, and Tailscale SSH are not part of this design. The Pi's host firewall retains its existing LAN and isolated-worker allowances, defaults to drop on forwarding, and has no Tailscale-to-rig transit rule. Preserve any pre-existing forwarding required by the local rig network; do not assume it can simply be disabled.

Tailscale membership is the first gate. Start with no broad ACLs and no grant, validate denials, then add only the operator-to-Pi-tag TCP 443 grant. Review the complete effective policy before either step: a second broad ACL or grant can defeat a narrow-looking example. Enable device approval, enroll the Pi and phone separately, and test loss/revocation of a phone. A MagicDNS/Serve URL is a route, not an authorization secret.

The mobile process binds to loopback and trusts identity headers only when reached through local Tailscale Serve. It rejects a missing or different Tailscale login and the wrong Host. Mutations require the configured HTTPS Origin, same-origin fetch metadata, and JSON with a route-specific size cap. It maps only named `/mobile/api/...` operations to gateway routes; the browser cannot supply an arbitrary upstream or upload destination path. The bounded `transfer-write` permission admits supported files to the generated Pi inbox, while `transfer-read` lists and downloads them; mobile deletion is not exposed. The loopback gateway requires an independent random mobile proxy token from a protected Pi-local environment file, so access to a broader gateway route cannot be obtained merely by changing a browser URL. A local process with access to that token remains inside the Pi trust boundary; this is not protection against a compromised Pi.

The LAN dashboard and the mobile dashboard have separate ingress and policy. The LAN browser certificate authorizes the full local operator surface. Mobile HTTP permissions (`MOBILE_PERMISSIONS`) and model-tool grants (`MOBILE_TOOL_SERVERS`) are configured separately. Local-model turns use the mobile tool list instead of inheriting the saved conversation's LAN tool grants. Mobile Codex turns force workspace permission and human approval. New privileged mobile operations need their own server-side authorization and action-specific approval; adding a UI button or broadening the route allowlist alone is insufficient. The phone and LAN browser read the same durable conversation store, so a turn can be continued across devices without copying session data to the phone.

## Adapt this deployment

1. Keep the existing LAN route healthy. Inspect its listener, certificate authentication, firewall, and SSH recovery path.
2. Create a dedicated tailnet or audit every existing policy entry. Replace any default allow-all ACL with an initial deny policy and test both positive and negative assertions. Keep real account and tag owners in a private policy copy.
3. Enroll only the Pi service node and the approved phone. Keep the GPU host and workers off the tailnet; confirm no advertised route, exit node, Tailscale SSH, or Funnel.
4. Install the mobile service on loopback. In a protected host environment file, set `MOBILE_TAILSCALE_USER`, `MOBILE_TAILSCALE_HOST`, and a fresh `MOBILE_PROXY_TOKEN` of at least 32 random characters. Give the mobile service and gateway only the file access they need. Do not log or commit values.
5. Configure Serve HTTPS 443 to the loopback mobile listener. Confirm its reported URL and tailnet-only mode. Apply the narrow 443 policy and validate that the phone can reach `/mobile/` over cellular.
6. Verify that direct SSH, NFS, manager, gateway, and rig routes remain denied from the phone. Verify that the LAN certificate dashboard, local SSH, rig control, worker mounts, and heartbeats still work. Exercise wrong identity, Host, Origin, disabled permissions, and device removal. Confirm one phone turn appears in the same conversation locally and survives a disconnect.

Use the service names and listener ports in [the rollout runbook](MOBILE_DASHBOARD.md) for this implementation. A machine or tailnet rename changes the HTTPS host: update `MOBILE_TAILSCALE_HOST`, restart the mobile process, recheck Serve and Origin validation, and update bookmarks. Do not treat the URL as permanent configuration.

## Remaining limits and recovery

The mobile UI covers status and conversation continuation, with a smaller set of controls than the local dashboard. Current app checks and the mobile allowlist do not create an operating-system sandbox for every downstream tool. The separate service identity, per-tool destination policy, and fresh approval path for broad remote agent authority remain future work. A pending Codex approval may require reconciliation after a gateway restart; verify this before relying on the phone as the sole approval device.

If the remote path behaves unexpectedly, remove the 443 grant or disable Serve first. Leave the LAN dashboard and isolated rig network in place, then inspect the policy, service logs, listener addresses, and host firewall. Revoking or removing a lost phone from the tailnet cuts off its network access; test the actual denial. Keep deployment-specific backup paths in private operations notes.
