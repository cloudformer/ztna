"""ztna server: one Lambda behind a Function URL — starts and terminates on-demand EC2 jump hosts you tunnel through.

A jump is one EC2 instance launched from a launch template into a chosen network profile (a subnet + security
group defined in Terraform), with a lease (how long it stays up, extendable from the page up to MAX_SESSION_MINUTES).
It is always terminated, never stopped, so it is stateless: the instance carries no state; configuration comes from
the launch template, and you connect through SSM Session Manager (no inbound, no SSH key). The jump self-terminates
when its lease runs out or after IDLE_SHUTDOWN_MINUTES with no SSM session.

This Lambda never connects to anything: it launches / terminates instances, writes the lease, and serves the page.
The page builds the connect command — a port-forward (map one service to localhost) or a SOCKS tunnel (VPN-like) —
from the jump and the target you pick.

The first half (JSON, request body, sign-in, routing, error mapping, timing) is the series' shared shell; the
routes are in the second half.

Sign-in: two methods, each on or off; a request passes when any method that is on accepts it.
    HTTP_AUTH=1        HTTP Basic (USER_NAME / PASSWORD). Identity = the user name.
    CLOUDFLARE_AUTH=1  Cloudflare Access: the Cf-Access-Jwt-Assertion header, an RS256 JWT checked against the team's
                       public keys, aud, issuer and expiry. Identity = the email in it.

    GET  / , /api/ping , /api/me
    GET  /api/config                 networks, targets, durations, idle shutdown, region, version
    GET  /api/jumps                  the running jumps, newest first, each with its lease end
    POST /api/jumps                  {"network", "minutes"}: launch a jump in that network profile for that long
    POST /api/jumps/<id>/extend      its lease + 1 hour, never past MAX_SESSION_MINUTES after launch
    POST /api/jumps/<id>/stop        terminate it

Layout: leases/<instance id>.json ({"until": ISO time, "by"}). The jump reads its own lease to self-terminate; the
bucket's lifecycle rule expires old leases.
"""
import base64
import datetime
import hashlib
import json
import os
import re
import time
import urllib.parse

import boto3

import idlefree_auth as auth
import idlefree_ui

BUCKET = os.environ.get("BUCKET", "")
PROJECT = os.environ.get("PROJECT", "")                 # the tag value every jump carries (and the Name prefix)
LAUNCH_TEMPLATE_ID = os.environ.get("LAUNCH_TEMPLATE_ID", "")
INSTANCE_TYPE = os.environ.get("INSTANCE_TYPE", "t4g.micro")
REGION = os.environ.get("AWS_REGION", "")
SSH_USER = os.environ.get("SSH_USER", "ec2-user")       # for the VPN-like SOCKS command
LEASES = "leases/"
NETWORKS = json.loads(os.environ.get("NETWORKS", "{}"))  # {name: {"subnets": [...], "groups": [...], "public": bool}}
DEFAULT_NETWORK = os.environ.get("DEFAULT_NETWORK", next(iter(NETWORKS), ""))
TARGETS = json.loads(os.environ.get("TARGETS", "[]"))    # ["db-host:3306", ...]: the map-mode dropdown
IDLE_SHUTDOWN_MINUTES = int(os.environ.get("IDLE_SHUTDOWN_MINUTES", "30"))
MAX_SESSION_MINUTES = int(os.environ.get("MAX_SESSION_MINUTES", "480"))
DURATIONS = [m for m in (60, 120, 240, 480) if m <= MAX_SESSION_MINUTES] or [MAX_SESSION_MINUTES]
DEFAULT_MINUTES = 240 if 240 in DURATIONS else DURATIONS[-1]
EXTEND_MINUTES = 60
MAX_JUMPS = int(os.environ.get("MAX_JUMPS", "3"))
LIST_LIMIT = 20

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "..", "__init__.py"), encoding="utf-8") as f:
    VERSION = re.search(r'__version__ = "([^"]+)"', f.read()).group(1)
with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
    INDEX_HTML = f.read().replace("__VERSION__", VERSION).replace("/* idlefree-ui */", idlefree_ui.CSS)

_HASHES = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()
                   for s in re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S))
CSP = (f"default-src 'none'; script-src {_HASHES}; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
PAGE_HEADERS = {"content-security-policy": CSP, "x-content-type-options": "nosniff",
                "referrer-policy": "no-referrer", "x-frame-options": "DENY"}

INSTANCE_ID = re.compile(r"i-[0-9a-f]{8,17}")

ec2 = boto3.client("ec2")
s3 = boto3.client("s3")


class BadRequest(Exception):
    pass


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


class Busy(Exception):
    """max_jumps are up already."""


# ---------------- shared: the same shell as the rest of the series ----------------

def _json(status, data):
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(data, ensure_ascii=False, default=str)}


def _body(event):
    raw = event.get("body") or "{}"
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw)
        data = json.loads(raw)
    except ValueError:
        raise BadRequest("request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("request body must be a JSON object")
    return data


ERRORS = [(BadRequest, 400), (Forbidden, 403), (NotFound, 404), (Busy, 409)]


def lambda_handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method, path = http.get("method", "GET"), urllib.parse.unquote(event.get("rawPath", "/"))
    problem = auth.problem()
    if problem:
        return _json(503, {"error": problem})
    signed = auth.identity(event)
    if signed is None and not auth.HTTP_AUTH:
        return _json(401, {"error": "sign in through Cloudflare Access", "http_auth": False})
    if method == "GET" and path in ("/", "/index.html"):
        return {"statusCode": 200, "body": INDEX_HTML,
                "headers": dict(PAGE_HEADERS, **{"content-type": "text/html; charset=utf-8", "cache-control": "no-cache"})}
    if signed is None:
        return _json(401, {"error": "bad credentials", "http_auth": True})
    event["identity"], event["auth_method"] = signed
    t0 = time.perf_counter()
    status, data = 404, {"error": f"no route: {method} {path}"}
    try:
        for route_method, pattern, handler in ROUTES:
            match = pattern.fullmatch(path)
            if match and route_method == method:
                out = handler(event, **match.groupdict())
                status, data = out if isinstance(out, tuple) else (200, out)
                break
    except Exception as e:  # noqa: BLE001
        status = next((s for cls, s in ERRORS if isinstance(e, cls)), 500)
        data = {"error": str(e) if status < 500 else f"{type(e).__name__}: {e}"}
    data.setdefault("ms", round((time.perf_counter() - t0) * 1000, 1))
    return _json(status, data)


# ---------------- the project's API: jumps = EC2 instances tagged with PROJECT ----------------

def _instance_id(jump_id):
    if not INSTANCE_ID.fullmatch(jump_id):
        raise BadRequest(f"jump id: an instance id (i-…), got {jump_id!r}")
    return jump_id


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(t):
    if isinstance(t, str):
        t = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
    return t.astimezone(datetime.timezone.utc)


def _lease(jump_id):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=f"{LEASES}{jump_id}.json")["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def _write_lease(jump_id, until, who):
    s3.put_object(Bucket=BUCKET, Key=f"{LEASES}{jump_id}.json", ContentType="application/json",
                  Body=json.dumps({"until": _iso(until), "by": who}).encode())


def _tags(instance):
    return {t["Key"]: t["Value"] for t in instance.get("Tags", [])}


def _summary(instance):
    tags = _tags(instance)
    jid = instance["InstanceId"]
    return {"id": jid, "network": tags.get("Network", ""), "state": instance["State"]["Name"],
            "launched": instance.get("LaunchTime"), "by": tags.get("StartedBy", ""),
            "until": (_lease(jid) or {}).get("until")}


def _running():
    """The project's jumps that are pending or running, newest first."""
    out = []
    pager = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "tag:Project", "Values": [PROJECT]},
                 {"Name": "instance-state-name", "Values": ["pending", "running"]}])
    for page in pager:
        for res in page["Reservations"]:
            out += res["Instances"]
    out.sort(key=lambda i: i.get("LaunchTime") or datetime.datetime.min, reverse=True)
    return out[:LIST_LIMIT]


def ping(event):
    return {"ok": True}


def me(event):
    return {"identity": event["identity"], "auth_method": event["auth_method"]}


def config(event):
    return {"networks": list(NETWORKS), "default_network": DEFAULT_NETWORK, "targets": TARGETS,
            "durations": DURATIONS, "default_minutes": DEFAULT_MINUTES, "extend_minutes": EXTEND_MINUTES,
            "max_session_minutes": MAX_SESSION_MINUTES, "idle_shutdown_minutes": IDLE_SHUTDOWN_MINUTES,
            "max_jumps": MAX_JUMPS, "region": REGION, "ssh_user": SSH_USER, "version": VERSION}


def list_jumps(event):
    return {"jumps": [_summary(i) for i in _running()]}


def start_jump(event):
    body = _body(event)
    network, minutes = body.get("network", DEFAULT_NETWORK), body.get("minutes", DEFAULT_MINUTES)
    if network not in NETWORKS:
        raise BadRequest(f"network: one of {', '.join(NETWORKS)}, got {network!r}")
    if minutes not in DURATIONS:
        raise BadRequest(f"minutes: one of {', '.join(map(str, DURATIONS))}, got {minutes!r}")
    if len(_running()) >= MAX_JUMPS:
        raise Busy(f"{MAX_JUMPS} jump(s) up already (max_jumps): terminate one first")
    net = NETWORKS[network]
    nic = {"DeviceIndex": 0, "SubnetId": net["subnets"][0], "Groups": net["groups"],
           "AssociatePublicIpAddress": bool(net.get("public", False))}
    name = f"{PROJECT}-{network}"
    resp = ec2.run_instances(
        LaunchTemplate={"LaunchTemplateId": LAUNCH_TEMPLATE_ID}, InstanceType=INSTANCE_TYPE,
        MinCount=1, MaxCount=1, NetworkInterfaces=[nic],
        TagSpecifications=[{"ResourceType": "instance", "Tags": [
            {"Key": "Project", "Value": PROJECT}, {"Key": "Network", "Value": network},
            {"Key": "StartedBy", "Value": event["identity"]}, {"Key": "Name", "Value": name}]}])
    inst = resp["Instances"][0]
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
    _write_lease(inst["InstanceId"], until, event["identity"])
    out = _summary(inst)
    out["until"] = _iso(until)
    print(json.dumps({"event": "jump_started", "id": inst["InstanceId"], "network": network,
                      "until": out["until"], "by": event["identity"]}))
    return 201, out


def extend_jump(event, jump_id):
    _instance_id(jump_id)
    inst = _one(jump_id)
    if inst["State"]["Name"] not in ("pending", "running"):
        raise BadRequest(f"jump {jump_id} is not up")
    now = datetime.datetime.now(datetime.timezone.utc)
    lease = _lease(jump_id)
    until = max(now, _utc(lease["until"]) if lease else now) + datetime.timedelta(minutes=EXTEND_MINUTES)
    limit = _utc(inst["LaunchTime"]) + datetime.timedelta(minutes=MAX_SESSION_MINUTES)
    if until > limit:
        if lease and _utc(lease["until"]) >= limit - datetime.timedelta(minutes=1):
            raise BadRequest(f"jump {jump_id} is at its limit, {MAX_SESSION_MINUTES} min after launch (max_session_minutes)")
        until = limit
    _write_lease(jump_id, until, event["identity"])
    print(json.dumps({"event": "jump_extended", "id": jump_id, "until": _iso(until), "by": event["identity"]}))
    return dict(_summary(inst), until=_iso(until))


def stop_jump(event, jump_id):
    _instance_id(jump_id)
    ec2.terminate_instances(InstanceIds=[jump_id])
    try:
        s3.delete_object(Bucket=BUCKET, Key=f"{LEASES}{jump_id}.json")
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps({"event": "jump_stopped", "id": jump_id, "by": event["identity"]}))
    return {"id": jump_id, "state": "shutting-down"}


def _one(jump_id):
    r = ec2.describe_instances(InstanceIds=[jump_id])["Reservations"]
    if not r or not r[0]["Instances"]:
        raise NotFound(f"no jump {jump_id}")
    inst = r[0]["Instances"][0]
    if _tags(inst).get("Project") != PROJECT:
        raise NotFound(f"no jump {jump_id}")
    return inst


JUMP = r"/api/jumps/(?P<jump_id>[^/]+)"
ROUTES = [
    ("GET",  re.compile(r"/api/ping"), ping),
    ("GET",  re.compile(r"/api/me"), me),
    ("GET",  re.compile(r"/api/config"), config),
    ("GET",  re.compile(r"/api/jumps"), list_jumps),
    ("POST", re.compile(r"/api/jumps"), start_jump),
    ("POST", re.compile(JUMP + "/extend"), extend_jump),
    ("POST", re.compile(JUMP + "/stop"), stop_jump),
]
