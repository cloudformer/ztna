# ztna

**Zero-trust, on-demand access to private resources: a persistent ≈ $0 console starts a stateless jump host in the
network you pick, you tunnel through it — map a port to your laptop, or route like a VPN — and it is terminated when
you are done.** Part of [IdleFree](https://cloudformer.github.io/) · 中文：[README.zh.md](README.zh.md)

Replaces a standing bastion / AWS Client VPN. No always-on jump host, no VPN client, no SSH keys, no inbound ports —
IAM + SSM Session Manager are the only gate, and nothing runs (or costs) while nobody is connected.

## Two ways to use it

| Mode | What it does | For |
|---|---|---|
| **Port-forward** (map) | a private service appears on a local port — `localhost:3306` *is* the private RDS | a local GUI tool or script against one resource |
| **VPN-like** (tunnel) | your traffic routes through the jump — reach the whole network, go out from its IP | browsing many internal services, or a session that exits from one IP |

Both use the **same jump host** and the **same SSM tunnel** — only how you connect from your laptop differs. One box,
two usages.

## Lifecycle — stateless, ≈ $0 idle

- The **console** is a Lambda behind a Function URL: always on, ≈ $0, the only persistent part.
- **Start** — pick a network profile; the console launches a jump host from an **EC2 launch template** in that
  network. A **lease** is written with the start time and a chosen duration.
- **No hard time limit** on the host (EC2); the lease is the cost guardrail. **Extend** it (+time) from the console
  while you work.
- **Idle shutdown** — no activity for `idle_shutdown_minutes` → the host is terminated.
- **Always terminated, never stopped.** The jump host is stateless and disposable: created fresh from the template,
  destroyed completely. Nothing is kept on it — configuration comes from the template (IaC), any results go to S3.
  A stopped, stateful box would defeat the point; IdleFree hosts are cattle, not pets.

## Network profiles — pre-defined, only selectable

Each profile is a subnet + security group(s) defined in Terraform. The console only **selects** among them; a public
web page can never define new network access.

| Profile (example) | Network | Reaches |
|---|---|---|
| `rds` | private subnet + RDS SG | only that RDS, port 3306/5432 |
| `redis` | private subnet + Redis SG | only Redis, 6379 |
| `internal` | private subnet + several SGs | RDS / Redis / Kafka |
| `internet` | public subnet, public IP | the internet (random IP), no private resources |

The network (subnet + SG) is chosen at launch; the sign-in gate and least-privilege scoping come from IaC, not the page.

## Zero trust

Per-session sign-in (console + IAM), access scoped to one profile (and, in map mode, one service), only while needed,
terminated after, and fully logged (IAM + SSM session logs). No standing network, no implicit trust, nothing left behind.

## Status

**Design.** This README is the plan; the console, launch template and Terraform module are not built yet.

## License

MIT
