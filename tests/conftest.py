"""Shared fixtures: the loaded contract and canonical SSM param sets."""

from __future__ import annotations

import json

import pytest

from longshore.contract import Contract, load_contract

ACCOUNT = "123456789012"
REGION = "us-east-2"
SERVICE = "myapp-staging"
PREFIX = f"/services/{SERVICE}"

EXECUTION_ROLE = f"arn:aws:iam::{ACCOUNT}:role/{SERVICE}-exec"
TASK_ROLE = f"arn:aws:iam::{ACCOUNT}:role/{SERVICE}-task"
CLUSTER_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/staging"
LOG_GROUP = f"/ecs/{SERVICE}"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/myapp:abc123"


@pytest.fixture(autouse=True)
def aws_test_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials so botocore signing works under moto and no test can
    ever reach a real AWS account."""
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture(scope="session")
def contract() -> Contract:
    return load_contract()


@pytest.fixture()
def minimal_params() -> dict[str, str]:
    """Only the always-present keys Terraform writes unconditionally."""
    return {
        "ecs/cluster_arn": CLUSTER_ARN,
        "ecs/subnets": json.dumps(["subnet-aaa", "subnet-bbb"]),
        "ecs/security_group_ids": json.dumps(["sg-111"]),
        "ecs/execution_role_arn": EXECUTION_ROLE,
        "ecs/task_role_arn": TASK_ROLE,
        "ecs/log_group_name": LOG_GROUP,
        "ecs/container_name": "main",
        "ecs/cpu": "256",
        "ecs/memory": "512",
        "ecs/cpu_architecture": "ARM64",
        "ecs/stop_timeout": "30",
        "ecs/init_process": "true",
        "ecs/container_port": "4000",
        "ecr/url": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/myapp",
        "kms/key_arn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/00000000",
    }


@pytest.fixture()
def full_params(minimal_params: dict[str, str]) -> dict[str, str]:
    """Every optional renderer key exercised at once."""
    return {
        **minimal_params,
        "ecs/cpu": "1024",
        "ecs/memory": "2048",
        "ecs/cpu_architecture": "X86_64",
        "ecs/stop_timeout": "60",
        "ecs/init_process": "false",
        "ecs/ephemeral_storage": "50",
        "ecs/command": json.dumps(["bundle", "exec", "puma"]),
        "ecs/ulimits": json.dumps([{"name": "nofile", "softLimit": 65536, "hardLimit": 65536}]),
        "ecs/health_check": json.dumps(
            {"command": ["curl", "-f", "http://localhost:4000/health"], "interval": 10}
        ),
        "ecs/service_connect_namespace_arn": (
            f"arn:aws:servicediscovery:{REGION}:{ACCOUNT}:namespace/ns-x"
        ),
        "ecs/service_connect_app_protocol": "http2",
        "ecs/sidecars": json.dumps(
            [
                {
                    "name": "log-router",
                    "image": "public.ecr.aws/aws-observability/aws-for-fluent-bit:2.32.0",
                    "essential": True,
                    "firelensConfiguration": {"type": "fluentbit"},
                    "secrets": [
                        {
                            "name": "PRESET",
                            "valueFrom": f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/preset",
                        }
                    ],
                }
            ]
        ),
    }
