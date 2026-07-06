"""SSM read paths against moto: config read + per-container env discovery."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from longshore.ssm import discover_env, discover_env_for, read_config

from .conftest import PREFIX, REGION


@pytest.fixture()
def ssm():
    with mock_aws():
        client = boto3.client("ssm", region_name=REGION)
        params = {
            f"{PREFIX}/ecs/cluster_arn": ("String", "arn:aws:ecs:::cluster/staging"),
            f"{PREFIX}/ecs/cpu": ("String", "256"),
            f"{PREFIX}/ecr/url": ("String", "repo-url"),
            f"{PREFIX}/containers/main/env/APP_ENV": ("String", "staging"),
            f"{PREFIX}/containers/main/env/DB_URL": ("SecureString", "postgres://..."),
            f"{PREFIX}/containers/main/env/nested/TOO_DEEP": ("String", "excluded"),
            f"{PREFIX}/containers/log-router/env/API_KEY": ("SecureString", "shhh"),
            "/services/other-service/ecs/cpu": ("String", "4096"),
        }
        for name, (ssm_type, value) in params.items():
            client.put_parameter(Name=name, Type=ssm_type, Value=value)
        yield client


def test_read_config_excludes_containers_subtree(ssm):
    config = read_config(ssm, PREFIX)
    assert config == {
        "ecs/cluster_arn": "arn:aws:ecs:::cluster/staging",
        "ecs/cpu": "256",
        "ecr/url": "repo-url",
    }


def test_read_config_does_not_leak_other_services(ssm):
    assert "ecs/cpu" not in read_config(ssm, "/services/nonexistent")
    assert read_config(ssm, "/services/other-service") == {"ecs/cpu": "4096"}


def test_discover_env_sorted_arns_only(ssm):
    entries = discover_env(ssm, PREFIX, "main")
    assert [e["name"] for e in entries] == ["APP_ENV", "DB_URL"]
    for entry in entries:
        assert entry["valueFrom"].startswith("arn:aws:ssm:")
        assert "parameter" in entry["valueFrom"]
        # ARNs only — never the value.
        assert set(entry) == {"name", "valueFrom"}


def test_discover_env_is_non_recursive(ssm):
    names = [e["name"] for e in discover_env(ssm, PREFIX, "main")]
    assert "TOO_DEEP" not in names


def test_discover_env_for_all_containers(ssm):
    env = discover_env_for(ssm, PREFIX, ["main", "log-router", "no-env-sidecar"])
    assert [e["name"] for e in env["main"]] == ["APP_ENV", "DB_URL"]
    assert [e["name"] for e in env["log-router"]] == ["API_KEY"]
    assert env["no-env-sidecar"] == []


def test_discover_env_pagination(ssm):
    for i in range(25):  # get_parameters_by_path pages at 10
        ssm.put_parameter(Name=f"{PREFIX}/containers/big/env/VAR_{i:02d}", Type="String", Value="v")
    entries = discover_env(ssm, PREFIX, "big")
    assert len(entries) == 25
    assert entries == sorted(entries, key=lambda e: e["name"])
