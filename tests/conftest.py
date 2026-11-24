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
    TARGETS=json.dumps(["static-extra:9999"]))

NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


# two VPCs: the default one with a private and a public subnet, and another that is empty
VPCS = [{"VpcId": "vpc-11111111", "CidrBlock": "172.31.0.0/16", "IsDefault": True, "Tags": []},
        {"VpcId": "vpc-22222222", "CidrBlock": "10.0.0.0/16", "IsDefault": False,
         "Tags": [{"Key": "Name", "Value": "prod"}]}]
SUBNETS = [{"SubnetId": "subnet-aaaa1111", "VpcId": "vpc-11111111", "AvailabilityZone": "us-east-2a",
            "CidrBlock": "172.31.0.0/20", "MapPublicIpOnLaunch": False, "Tags": []},
           {"SubnetId": "subnet-bbbb2222", "VpcId": "vpc-11111111", "AvailabilityZone": "us-east-2b",
            "CidrBlock": "172.31.16.0/20", "MapPublicIpOnLaunch": True, "Tags": []}]
GROUPS = [{"GroupId": "sg-def11111", "GroupName": "default", "VpcId": "vpc-11111111"},
          {"GroupId": "sg-def22222", "GroupName": "default", "VpcId": "vpc-22222222"}]


class FakeEC2:
    def __init__(self):
        self.instances = {}
        self.runs = []

    def describe_vpcs(self):
        return {"Vpcs": VPCS}

    def describe_subnets(self, SubnetIds=None):
        if SubnetIds is None:
            return {"Subnets": SUBNETS}
        return {"Subnets": [s for s in SUBNETS if s["SubnetId"] in SubnetIds]}

    def describe_security_groups(self, Filters):
        vpcs = next(f["Values"] for f in Filters if f["Name"] == "vpc-id")
        names = next(f["Values"] for f in Filters if f["Name"] == "group-name")
        return {"SecurityGroups": [g for g in GROUPS if g["VpcId"] in vpcs and g["GroupName"] in names]}

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


class FakeRDS:
    """One MySQL in the default VPC, one Postgres in the other."""

    def describe_db_instances(self):
        return {"DBInstances": [
            {"DBInstanceIdentifier": "db-a", "Endpoint": {"Address": "db-a.abc.us-east-2.rds.example.com", "Port": 3306},
             "DBSubnetGroup": {"VpcId": "vpc-11111111"}},
            {"DBInstanceIdentifier": "db-b", "Endpoint": {"Address": "db-b.abc.us-east-2.rds.example.com", "Port": 5432},
             "DBSubnetGroup": {"VpcId": "vpc-22222222"}}]}


class FakeElastiCache:
    """One redis in the default VPC."""

    def describe_cache_subnet_groups(self):
        return {"CacheSubnetGroups": [{"CacheSubnetGroupName": "redis-net", "VpcId": "vpc-11111111"}]}

    def describe_cache_clusters(self, ShowCacheNodeInfo):
        return {"CacheClusters": [
            {"CacheClusterId": "redis-a", "CacheSubnetGroupName": "redis-net", "ConfigurationEndpoint": None,
             "CacheNodes": [{"Endpoint": {"Address": "redis-a.abc.cache.example.com", "Port": 6379}}]}]}


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
