"""The ztna Lambda: sign-in, launch / list / extend / terminate jumps, lease, config."""
import json

import pytest

from conftest import FakeEC2, FakeElastiCache, FakeRDS, FakeS3, basic
from ztna.server import lambda_function as lf


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    ec2, s3 = FakeEC2(), FakeS3()
    monkeypatch.setattr(lf, "ec2", ec2)
    monkeypatch.setattr(lf, "s3", s3)
    monkeypatch.setattr(lf, "rds", FakeRDS())
    monkeypatch.setattr(lf, "elasticache", FakeElastiCache())
    return ec2, s3


def call(method, path, body=None, auth=True, query=None):
    event = {"requestContext": {"http": {"method": method}}, "rawPath": path,
             "headers": {"authorization": basic()} if auth else {}, "queryStringParameters": query}
    if body is not None:
        event["body"] = json.dumps(body)
    r = lf.lambda_handler(event, None)
    if r["headers"]["content-type"] != "application/json":
        return r["statusCode"], r
    data = json.loads(r["body"])
    data.pop("ms", None)
    return r["statusCode"], data


def start(subnet="subnet-aaaa1111", minutes=240):
    return call("POST", "/api/jumps", {"subnet": subnet, "minutes": minutes})


def jumps():
    return call("GET", "/api/jumps")[1]["jumps"]


def test_page_is_public_and_carries_the_version():
    status, r = call("GET", "/", auth=False)
    assert status == 200 and lf.VERSION in r["body"] and "script-src 'sha256-" in r["headers"]["content-security-policy"]


def test_api_needs_sign_in():
    assert call("GET", "/api/jumps", auth=False)[0] == 401


def test_config():
    status, r = call("GET", "/api/config")
    assert status == 200 and r["region"] == "us-east-2"
    assert r["max_jumps"] == 2 and r["idle_shutdown_minutes"] == 30 and r["version"] == lf.VERSION


def test_networks_lists_vpcs_and_their_subnets():
    status, r = call("GET", "/api/networks")
    assert status == 200 and [v["id"] for v in r["vpcs"]] == ["vpc-11111111", "vpc-22222222"]  # default first
    sn = r["vpcs"][0]["subnets"]
    assert [x["id"] for x in sn] == ["subnet-aaaa1111", "subnet-bbbb2222"]
    assert sn[0]["public"] is False and sn[1]["public"] is True and r["vpcs"][1]["name"] == "prod"


def test_targets_are_the_vpcs_endpoints_plus_the_static_extras():
    status, r = call("GET", "/api/targets", query={"vpc": "vpc-11111111"})
    assert status == 200
    assert r["targets"] == ["db-a.abc.us-east-2.rds.example.com:3306",
                            "redis-a.abc.cache.example.com:6379", "static-extra:9999"]
    assert call("GET", "/api/targets", query={"vpc": "vpc-22222222"})[1]["targets"] == [
        "db-b.abc.us-east-2.rds.example.com:5432", "static-extra:9999"]
    assert call("GET", "/api/targets", query={"vpc": "oops"})[0] == 400


def test_no_jumps_at_first():
    assert jumps() == []


def test_start_jump(aws):
    ec2, s3 = aws
    status, j = start("subnet-aaaa1111")
    assert status == 201 and j["network"] == "subnet-aaaa1111" and j["vpc"] == "vpc-11111111"
    assert j["state"] == "pending" and j["until"]
    assert j["id"] in ec2.instances                                   # the instance was launched
    assert f"leases/{j['id']}.json" in s3.objects                     # with a lease
    nic = ec2.runs[0]["nic"]
    assert nic["SubnetId"] == "subnet-aaaa1111" and nic["AssociatePublicIpAddress"] is False
    assert nic["Groups"] == ["sg-def11111"]                           # the VPC's default group
    assert [x["id"] for x in jumps()] == [j["id"]]


def test_public_subnet_gets_a_public_ip(aws):
    ec2, _ = aws
    start("subnet-bbbb2222")
    assert ec2.runs[0]["nic"]["SubnetId"] == "subnet-bbbb2222" and ec2.runs[0]["nic"]["AssociatePublicIpAddress"] is True


@pytest.mark.parametrize("body", [{"subnet": "bogus", "minutes": 240}, {"subnet": "subnet-aaaa1111", "minutes": 7}])
def test_bad_start_is_rejected(body, aws):
    assert call("POST", "/api/jumps", body)[0] == 400 and jumps() == []


def test_unknown_subnet_is_not_found(aws):
    assert call("POST", "/api/jumps", {"subnet": "subnet-00000000", "minutes": 240})[0] == 404


def test_max_jumps(aws):
    start(); start()
    assert start()[0] == 409 and len(jumps()) == 2       # MAX_JUMPS = 2


def test_extend_moves_the_lease(aws):
    _, s3 = aws
    j = start()[1]
    before = json.loads(s3.objects[f"leases/{j['id']}.json"])["until"]
    status, d = call("POST", f"/api/jumps/{j['id']}/extend")
    assert status == 200 and d["until"] >= before


def test_terminate(aws):
    ec2, s3 = aws
    j = start()[1]
    status, d = call("POST", f"/api/jumps/{j['id']}/stop")
    assert status == 200 and ec2.instances[j["id"]]["State"]["Name"] == "shutting-down"
    assert f"leases/{j['id']}.json" not in s3.objects      # lease cleaned up


def test_bad_jump_id():
    assert call("POST", "/api/jumps/not-an-instance/stop")[0] == 400
    assert call("POST", "/api/jumps/i-0000000000000000/extend")[0] == 404
