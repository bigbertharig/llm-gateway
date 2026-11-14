# Tailscale activation preparation

Status: prepared on 2026-11-13; no Pi enrollment, Serve, or Funnel is active.
This runbook is for the later networking session. The existing dashboard on
the home LAN is an administrative surface, not the remote harness session UI.

## Prepared state

- Tailscale 1.104.6 is installed on the Pi; `tailscaled` is disabled and stopped.
- The Pi host firewall drops inbound traffic outside explicit LAN and private-rig
  allowances. Its forwarding policy drops all transit traffic. There is no
  `tailscale0` allowance yet. Outbound connections remain available.
- Pi SSH uses keys only, disables root login and agent/X11 forwarding, and keeps
  TCP forwarding for the operator tunnel fallback. The NFS export and eth0
  TCP/2049 ingress admit the isolated 10.0.0.0/24 rig and Orange Pi worker
  network, as approved on 2026-11-14. Wi-Fi and Tailscale do not admit NFS.
- The gateway and harness session front door is still a design. Tailscale
  enrollment alone must not publish the existing broad dashboard.

## Before enrolling the Pi

1. Inspect the existing tailnet policy and devices in the admin console. Save a
   private backup of that policy. Do not overwrite an active tailnet's grants
   with the example file: doing so may interrupt other devices.
2. Remove any default or legacy allow-all ACL that would override narrow grants.
   Start with a policy that grants the Pi no inbound access. Check the admin
   console's policy tests, including denial from an unapproved user and denial
   of Pi SSH, manager, gateway API, NFS, and rig routes. The example
   `deploy/tailscale-policy.hujson.template` shows the later 443-only session
   grant; do not apply it until the session front door exists.
3. Decide the operator identity and Pi tag/device ownership, then configure
   device approval and key-expiry policy. Record real account, device, and
   recovery details in the private operations store, not this repository.
4. Confirm a working local SSH path and an operator rollback path. Verify
   `pi-host-ingress.service`, the gateway, and the rig NFS mount are healthy.

## First enrollment and network check

1. Enroll only the Pi with the restrictive policy. Keep Serve, Funnel, subnet
   routes, exit-node advertising, and IP forwarding off. Do not expose the rig.
2. Confirm tailnet membership and outbound coordination. Check the Pi's
   `tailscale0` interface and effective host firewall rules. From another
   tailnet device, prove inbound attempts are denied. From the LAN, prove SSH
   and client-certificate dashboard access still work; from the rig, prove NFS
   still works. Check `nft` and Tailscale logs for unintended drops.
3. Only when the separate authenticated harness session listener exists, merge
   a 443-only operator grant with explicit denial tests. Configure Serve for
   that loopback listener, never for the administrative dashboard. With the
   host firewall active, test access from an approved and an unapproved device;
   add a narrowly scoped `tailscale0` ingress rule only if the measured Serve
   path requires it. Keep forwarding drop and do not permit NFS or manager ports.
4. Verify session authentication, authorization, origin/CSRF defenses, and
   revocation before relying on phone access. A tailnet identity is a network
   gate, not the application's action authorization.

The first networking session can finish at denied-by-default enrollment. A
working remote chat session also needs the separate session front door and
its application security checks.

References: [Tailscale policy syntax](https://tailscale.com/docs/reference/syntax/policy-file),
[grants](https://tailscale.com/docs/reference/syntax/grants),
[access control](https://tailscale.com/docs/features/access-control/acls),
[Serve](https://tailscale.com/docs/features/tailscale-serve), and
[firewall ports](https://tailscale.com/docs/reference/faq/firewall-ports).
