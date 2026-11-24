"""ztna — zero-trust, on-demand access to private resources. A persistent ≈ $0 Lambda console starts a stateless EC2
jump host in a chosen network profile; you tunnel through it over SSM Session Manager — a port-forward to your laptop,
or a SOCKS tunnel (VPN-like) — and it is terminated when you are done.

ztna.server is the Lambda (the web console and its API). The Terraform module cloudformer/infra/aws//modules/ztna
deploys it along with the launch template, the network profiles and the IAM.
"""

__version__ = "0.2.0"
