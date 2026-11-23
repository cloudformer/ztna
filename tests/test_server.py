"""The ztna Lambda: sign-in, launch / list / extend / terminate jumps, lease, config."""
import json

import pytest

from conftest import FakeEC2, FakeS3, basic
from ztna.server import lambda_function as lf


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    ec2, s3 = FakeEC2(), FakeS3()
    monkeypatch.setattr(lf, "ec2", ec2)
    monkeypatch.setattr(lf, "s3", s3)
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


def start(network="database", minutes=240):
    return call("POST", "/api/jumps", {"network": network, "minutes": minutes})


def jumps():
    return call("GET", "/api/jumps")[1]["jumps"]


def test_page_is_public_and_carries_the_version():
    status, r = call("GET", "/", auth=False)
    assert status == 200 and lf.VERSION in r["body"] and "script-src 'sha256-" in r["headers"]["content-security-policy"]


def test_api_needs_sign_in():
    assert call("GET", "/api/jumps", auth=False)[0] == 401


def test_config():
    status, r = call("GET", "/api/config")
    assert status == 200
    assert set(r["networks"]) == {"database", "internet"} and r["default_network"] == "database"
    assert r["targets"] == ["db-primary:3306", "cache-01:6379"] and r["region"] == "us-east-2"
    assert r["max_jumps"] == 2 and r["idle_shutdown_minutes"] == 30 and r["version"] == lf.VERSION


def test_no_jumps_at_first():
    assert jumps() == []


def test_start_jump(aws):
    ec2, s3 = aws
    status, j = start("database")
    assert status == 201 and j["network"] == "database" and j["state"] == "pending" and j["until"]
    assert j["id"] in ec2.instances                                   # the instance was launched
    assert f"leases/{j['id']}.json" in s3.objects                     # with a lease
    assert ec2.runs[0]["nic"]["SubnetId"] == "subnet-1" and ec2.runs[0]["nic"]["AssociatePublicIpAddress"] is False
    assert [x["id"] for x in jumps()] == [j["id"]]


def test_public_network_gets_a_public_ip(aws):
    ec2, _ = aws
    start("internet")
    assert ec2.runs[0]["nic"]["SubnetId"] == "subnet-2" and ec2.runs[0]["nic"]["AssociatePublicIpAddress"] is True


@pytest.mark.parametrize("body", [{"network": "bogus", "minutes": 240}, {"network": "database", "minutes": 7}])
def test_bad_start_is_rejected(body, aws):
    assert call("POST", "/api/jumps", body)[0] == 400 and jumps() == []


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
