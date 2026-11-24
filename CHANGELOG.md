# Changelog

## 0.2.3 — 2026-11-24

- Charcoal theme (picked 3c of four shades) as the default, with a ☀/☾ toggle in the header: light = the
  idlefree-ui default theme, remembered per browser.
- One port on screen: the Target dropdown shows hosts only, and picking one drops its port into **Local port**.
- Terminate asks first — a dialog with the instance id and how many active SSM sessions would drop; the safe
  button has focus.

## 0.2.2 — 2026-11-24

- The machine card splits in two: machine on the left (id · state · network · lease), its live SSM sessions as
  their own right-hand column, one per line — left is the box, right is who is connected through it.
- Docs caught up: SOCKS naming everywhere, console.svg redrawn to the moss UI, the sessions column included.

## 0.2.1 — 2026-11-24

- The console redesigned: mission-control panels in a soft moss-green terminal palette (low contrast, mono
  throughout). Jumps render as machine cards that say what they are — EC2 · state · network · lease — and
  carry their live SSM sessions.
- Every jump lists its **active SSM sessions** (`ssm:DescribeSessions`, read-only): who, mode, target, minutes.
  Generated commands carry `--reason ztna:<user>:<mode>:<browser token>:<target>`; the token is 6 random chars
  minted once per browser, so the list marks the sessions started from your machine as YOU. Port-forward and
  SOCKS sessions both show; sessions opened outside the console still list by their IAM owner.
- Modes renamed to the proper terms: **Port forward** and **SOCKS proxy** (`ssh -D`, dynamic forwarding). In
  SOCKS mode the Target dropdown is disabled — the target is all traffic, 0.0.0.0/0 through the jump.
- A **Local port** box in port-forward mode, prefilled with the target's own port: forward two databases at
  once by giving the second one another local port (db-a → 3306, db-b → 3307).

## 0.2.0 — 2026-11-23

- Launch by picking a VPC and a subnet, discovered live (`GET /api/networks`), instead of network profiles
  hand-written in Terraform: the jump gets the VPC's default security group, and a public IP exactly when the
  subnet assigns them. The `networks` / `default_network` variables are gone from the module and the caller.
- The map-mode Target dropdown now lists the RDS and ElastiCache endpoints of the picked jump's VPC
  (`GET /api/targets?vpc=…`), with the `targets` variable kept as static extras; a custom host:port still works.
- The SOCKS command pushes your SSH key first (`aws ec2-instance-connect send-ssh-public-key`, valid 60 s):
  the jump ships no authorized keys, so the bare `ssh -D` was always `Permission denied` — found on the first
  real connect. Map mode is pure SSM port forwarding and never needed a key.
- A Profile box next to the mode switch: filled in, every generated command carries `--profile <name>`, so it
  pastes straight into a terminal whose default profile is a different account. Remembered per browser.

## 0.1.0 — 2026-11-23

- First release: a Lambda console (Function URL) that launches on-demand EC2 jump hosts from a launch template into
  a chosen network profile (subnet + security group, defined in Terraform), writes each one's lease, and terminates
  them — always terminated, never stopped, so the jump is stateless. You connect through SSM Session Manager
  (no inbound, no SSH key): a port-forward that maps a private service to your laptop, or a SOCKS tunnel (VPN-like).
  A jump self-terminates when its lease runs out or after `idle_shutdown_minutes` with no SSM session.
- Sign-in with a password (HTTP Basic) or Cloudflare Access, the same as the rest of IdleFree. ≈ $0 while no jump is up.
