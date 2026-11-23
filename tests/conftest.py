"""Fakes for the AWS clients the Lambda uses (EC2 jumps, S3 lease): no AWS account needed."""
import base64
import datetime
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIBLINGS = os.path.dirname(ROOT)   # repo-private: idlefree-auth / idlefree-ui live next to ztna
sys.path[:0] = [os.path.join(ROOT, "client"),
                os.path.join(SIBLINGS, "idlefree-auth"), os.path.join(SIBLINGS, "idlefree-ui")]
os.environ.update(
    AWS_REGION="us-east-2", AWS_DEFAULT_REGION="us-east-2",
    HTTP_AUTH="1", USER_NAME="admin", PASSWORD="a-password-of-16+chars",
    BUCKET="test-ztna", PROJECT="test-ztna", LAUNCH_TEMPLATE_ID="lt-0123456789abcdef0",
    INSTANCE_TYPE="t4g.micro", SSH_USER="ec2-user",
    IDLE_SHUTDOWN_MINUTES="30", MAX_SESSION_MINUTES="480", MAX_JUMPS="2",
    NETWORKS=json.dumps({"database": {"subnets": ["subnet-1"], "groups": ["sg-1"], "public": False},
                         "internet": {"subnets": ["subnet-2"], "groups": ["sg-2"], "public": True}}),
    DEFAULT_NETWORK="database",
    TARGETS=json.dumps(["db-primary:3306", "cache-01:6379"]))

NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


class FakeEC2:
    def __init__(self):
        self.instances = {}
        self.runs = []

    def run_instances(self, LaunchTemplate, InstanceType, MinCount, MaxCount, NetworkInterfaces, TagSpecifications):
        iid = "i-0%015x" % len(self.instances)
        tags = list(TagSpecifications[0]["Tags"])
        inst = {"InstanceId": iid, "State": {"Name": "pending"}, "LaunchTime": NOW, "Tags": tags}
        self.instances[iid] = inst
        self.runs.append({"nic": NetworkInterfaces[0], "type": InstanceType, "lt": LaunchTemplate})
        return {"Instances": [inst]}

    def get_paginator(self, name):
        insts = self.instances

        class Paginator:
            def paginate(self, Filters):
                proj = next((f["Values"] for f in Filters if f["Name"] == "tag:Project"), [None])
                states = next((f["Values"] for f in Filters if f["Name"] == "instance-state-name"), None)
                out = []
                for i in insts.values():
                    tags = {t["Key"]: t["Value"] for t in i["Tags"]}
                    if tags.get("Project") in proj and (states is None or i["State"]["Name"] in states):
                        out.append(i)
                yield {"Reservations": [{"Instances": [i]} for i in out]}
        return Paginator()

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [self.instances[i]]} for i in InstanceIds if i in self.instances]}

    def terminate_instances(self, InstanceIds):
        for i in InstanceIds:
            if i in self.instances:
                self.instances[i]["State"] = {"Name": "shutting-down"}
        return {}


class FakeS3:
    class exceptions:
        NoSuchKey = type("NoSuchKey", (Exception,), {})

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfNoneMatch=None):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}


def basic(user="admin", password="a-password-of-16+chars"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
