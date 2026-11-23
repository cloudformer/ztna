# Changelog

## 0.1.0 — 2026-11-23

- First release: a Lambda console (Function URL) that launches on-demand EC2 jump hosts from a launch template into
  a chosen network profile (subnet + security group, defined in Terraform), writes each one's lease, and terminates
  them — always terminated, never stopped, so the jump is stateless. You connect through SSM Session Manager
  (no inbound, no SSH key): a port-forward that maps a private service to your laptop, or a SOCKS tunnel (VPN-like).
  A jump self-terminates when its lease runs out or after `idle_shutdown_minutes` with no SSM session.
- Sign-in with a password (HTTP Basic) or Cloudflare Access, the same as the rest of IdleFree. ≈ $0 while no jump is up.
