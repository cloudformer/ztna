"""ztna server: one Lambda behind a Function URL — starts and terminates on-demand EC2 jump hosts you tunnel through.

A jump is one EC2 instance launched from a launch template into a subnet you pick on the page (VPC and subnet
dropdowns, discovered live; the VPC's default security group), with a lease (how long it stays up, extendable from the page up to MAX_SESSION_MINUTES).
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
    GET  /api/config                 durations, idle shutdown, region, version
    GET  /api/networks               the VPCs and their subnets, discovered live (the launch dropdowns)
    GET  /api/targets?vpc=vpc-…      RDS + ElastiCache endpoints in that VPC, plus the static extras (map-mode dropdown)
    GET  /api/jumps                  the running jumps, newest first, each with its lease end and its active SSM
                                     sessions (owner, reason, start) — the page marks sessions whose --reason carries
                                     its own browser token (ztna:<user>:<mode>:<6 chars>) as yours
    POST /api/jumps                  {"subnet", "minutes"}: launch a jump into that subnet for that long
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
TARGETS = json.loads(os.environ.get("TARGETS", "[]"))   # ["db-host:3306", ...]: static extras for the map-mode dropdown
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
SUBNET_ID = re.compile(r"subnet-[0-9a-f]{8,17}")
VPC_ID = re.compile(r"vpc-[0-9a-f]{8,17}")

ec2 = boto3.client("ec2")
s3 = boto3.client("s3")
rds = boto3.client("rds")
elasticache = boto3.client("elasticache")
ssm = boto3.client("ssm")


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
    return {"id": jid, "network": tags.get("Network", ""), "vpc": tags.get("Vpc", ""), "state": instance["State"]["Name"],
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
    return {"durations": DURATIONS, "default_minutes": DEFAULT_MINUTES, "extend_minutes": EXTEND_MINUTES,
            "max_session_minutes": MAX_SESSION_MINUTES, "idle_shutdown_minutes": IDLE_SHUTDOWN_MINUTES,
            "max_jumps": MAX_JUMPS, "region": REGION, "ssh_user": SSH_USER, "version": VERSION}


def _name(obj):
    return {t["Key"]: t["Value"] for t in obj.get("Tags", [])}.get("Name", "")


def networks(event):
    """The launch dropdowns: every VPC and its subnets, discovered live."""
    vpcs = {}
    for v in ec2.describe_vpcs()["Vpcs"]:
        vpcs[v["VpcId"]] = {"id": v["VpcId"], "name": _name(v), "cidr": v.get("CidrBlock", ""),
                            "default": v.get("IsDefault", False), "subnets": []}
    for sn in ec2.describe_subnets()["Subnets"]:
        if sn["VpcId"] in vpcs:
            vpcs[sn["VpcId"]]["subnets"].append(
                {"id": sn["SubnetId"], "name": _name(sn), "az": sn["AvailabilityZone"],
                 "cidr": sn.get("CidrBlock", ""), "public": sn.get("MapPublicIpOnLaunch", False)})
    for v in vpcs.values():
        v["subnets"].sort(key=lambda x: x["az"])
    return {"vpcs": sorted(vpcs.values(), key=lambda x: (not x["default"], x["id"]))}


def _discovered(vpc):
    """RDS + ElastiCache endpoints living in that VPC, as host:port."""
    out = []
    for db in rds.describe_db_instances()["DBInstances"]:
        ep = db.get("Endpoint") or {}
        if (db.get("DBSubnetGroup") or {}).get("VpcId") == vpc and ep.get("Address"):
            out.append(f"{ep['Address']}:{ep['Port']}")
    groups = {g["CacheSubnetGroupName"]: g["VpcId"]
              for g in elasticache.describe_cache_subnet_groups()["CacheSubnetGroups"]}
    for c in elasticache.describe_cache_clusters(ShowCacheNodeInfo=True)["CacheClusters"]:
        if groups.get(c.get("CacheSubnetGroupName")) != vpc:
            continue
        ep = c.get("ConfigurationEndpoint") or next(
            (n["Endpoint"] for n in c.get("CacheNodes", []) if n.get("Endpoint")), None)
        if ep:
            out.append(f"{ep['Address']}:{ep['Port']}")
    return out


def targets(event):
    vpc = (event.get("queryStringParameters") or {}).get("vpc", "")
    if not VPC_ID.fullmatch(vpc):
        raise BadRequest(f"vpc: a vpc id (vpc-…), got {vpc!r}")
    seen, merged = set(), []
    for t in _discovered(vpc) + TARGETS:
        if t not in seen:
            seen.add(t)
            merged.append(t)
    return {"targets": merged}


def _sessions(jump_id):
    """The active SSM sessions on that jump: who is connected right now."""
    try:
        found = ssm.describe_sessions(State="Active", Filters=[{"key": "Target", "value": jump_id}])["Sessions"]
    except Exception:  # noqa: BLE001  (a jump with sessions unreadable still lists)
        return []
    return [{"id": x.get("SessionId", ""), "owner": x.get("Owner", "").rsplit("/", 1)[-1],
             "reason": x.get("Reason", ""), "started": x.get("StartDate"),
             "document": x.get("DocumentName", "")} for x in found]


def list_jumps(event):
    return {"jumps": [dict(_summary(i), sessions=_sessions(i["InstanceId"])) for i in _running()]}


def start_jump(event):
    body = _body(event)
    subnet, minutes = body.get("subnet", ""), body.get("minutes", DEFAULT_MINUTES)
    if not SUBNET_ID.fullmatch(str(subnet)):
        raise BadRequest(f"subnet: a subnet id (subnet-…), got {subnet!r}")
    if minutes not in DURATIONS:
        raise BadRequest(f"minutes: one of {', '.join(map(str, DURATIONS))}, got {minutes!r}")
    if len(_running()) >= MAX_JUMPS:
        raise Busy(f"{MAX_JUMPS} jump(s) up already (max_jumps): terminate one first")
    found = ec2.describe_subnets(SubnetIds=[subnet])["Subnets"]
    if not found:
        raise NotFound(f"no subnet {subnet}")
    sn = found[0]
    vpc = sn["VpcId"]
    sg = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [vpc]}, {"Name": "group-name", "Values": ["default"]}])["SecurityGroups"]
    if not sg:
        raise NotFound(f"no default security group in {vpc}")
    nic = {"DeviceIndex": 0, "SubnetId": subnet, "Groups": [sg[0]["GroupId"]],
           "AssociatePublicIpAddress": bool(sn.get("MapPublicIpOnLaunch", False))}
    resp = ec2.run_instances(
        LaunchTemplate={"LaunchTemplateId": LAUNCH_TEMPLATE_ID}, InstanceType=INSTANCE_TYPE,
        MinCount=1, MaxCount=1, NetworkInterfaces=[nic],
        TagSpecifications=[{"ResourceType": "instance", "Tags": [
            {"Key": "Project", "Value": PROJECT}, {"Key": "Network", "Value": subnet}, {"Key": "Vpc", "Value": vpc},
            {"Key": "StartedBy", "Value": event["identity"]}, {"Key": "Name", "Value": f"{PROJECT}-{subnet}"}]}])
    inst = resp["Instances"][0]
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
    _write_lease(inst["InstanceId"], until, event["identity"])
    out = _summary(inst)
    out["until"] = _iso(until)
    print(json.dumps({"event": "jump_started", "id": inst["InstanceId"], "subnet": subnet, "vpc": vpc,
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
    ("GET",  re.compile(r"/api/networks"), networks),
    ("GET",  re.compile(r"/api/targets"), targets),
    ("GET",  re.compile(r"/api/jumps"), list_jumps),
    ("POST", re.compile(r"/api/jumps"), start_jump),
    ("POST", re.compile(JUMP + "/extend"), extend_jump),
    ("POST", re.compile(JUMP + "/stop"), stop_jump),
]
