"""Pure-function coverage of the renderer."""

from __future__ import annotations

import json

import pytest

from longshore.render import RenderError, container_names, render_task_definition

from .conftest import EXECUTION_ROLE, IMAGE, LOG_GROUP, REGION, SERVICE, TASK_ROLE

ENV_MAIN = [
    {"name": "APP_ENV", "valueFrom": "arn:aws:ssm:us-east-2:123456789012:parameter/a"},
    {"name": "DB_URL", "valueFrom": "arn:aws:ssm:us-east-2:123456789012:parameter/b"},
]
ENV_ROUTER = [
    {"name": "API_KEY", "valueFrom": "arn:aws:ssm:us-east-2:123456789012:parameter/c"},
]


def render(params, contract, env=None, image=IMAGE):
    return render_task_definition(SERVICE, image, params, env or {}, REGION, contract)


class TestMinimal:
    def test_minimal_render(self, minimal_params, contract):
        td = render(minimal_params, contract)
        assert td == {
            "family": SERVICE,
            "requiresCompatibilities": ["FARGATE"],
            "networkMode": "awsvpc",
            "cpu": "256",
            "memory": "512",
            "executionRoleArn": EXECUTION_ROLE,
            "taskRoleArn": TASK_ROLE,
            "runtimePlatform": {
                "cpuArchitecture": "ARM64",
                "operatingSystemFamily": "LINUX",
            },
            "containerDefinitions": [
                {
                    "name": "main",
                    "image": IMAGE,
                    "essential": True,
                    "stopTimeout": 30,
                    "linuxParameters": {"initProcessEnabled": True},
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": LOG_GROUP,
                            "awslogs-region": REGION,
                            "awslogs-stream-prefix": "main",
                        },
                    },
                    "portMappings": [{"containerPort": 4000, "protocol": "tcp"}],
                }
            ],
        }

    def test_empty_params_fail(self, contract):
        with pytest.raises(RenderError, match="no SSM parameters"):
            render({}, contract)

    def test_missing_required_lists_all(self, minimal_params, contract):
        del minimal_params["ecs/execution_role_arn"]
        del minimal_params["ecs/log_group_name"]
        with pytest.raises(RenderError) as exc:
            render(minimal_params, contract)
        assert "ecs/execution_role_arn" in str(exc.value)
        assert "ecs/log_group_name" in str(exc.value)

    def test_unknown_keys_are_ignored(self, minimal_params, contract):
        minimal_params["ecs/some_future_key"] = "whatever"
        minimal_params["alb/target_group_arn"] = "arn:aws:elasticloadbalancing:..."
        td = render(minimal_params, contract)
        assert "some_future_key" not in json.dumps(td)


class TestPorts:
    def test_no_port_no_port_mappings(self, minimal_params, contract):
        del minimal_params["ecs/container_port"]
        td = render(minimal_params, contract)
        assert "portMappings" not in td["containerDefinitions"][0]

    def test_service_connect_adds_name_and_protocol(self, minimal_params, contract):
        minimal_params["ecs/service_connect_namespace_arn"] = "arn:ns"
        td = render(minimal_params, contract)
        assert td["containerDefinitions"][0]["portMappings"] == [
            {"containerPort": 4000, "protocol": "tcp", "name": "main", "appProtocol": "http"}
        ]

    def test_service_connect_explicit_protocol(self, minimal_params, contract):
        minimal_params["ecs/service_connect_namespace_arn"] = "arn:ns"
        minimal_params["ecs/service_connect_app_protocol"] = "grpc"
        td = render(minimal_params, contract)
        assert td["containerDefinitions"][0]["portMappings"][0]["appProtocol"] == "grpc"

    def test_no_service_connect_no_port_name(self, minimal_params, contract):
        td = render(minimal_params, contract)
        mapping = td["containerDefinitions"][0]["portMappings"][0]
        assert "name" not in mapping and "appProtocol" not in mapping


class TestEnv:
    def test_primary_env_becomes_secrets(self, minimal_params, contract):
        td = render(minimal_params, contract, env={"main": ENV_MAIN})
        assert td["containerDefinitions"][0]["secrets"] == ENV_MAIN

    def test_no_env_no_secrets_key(self, minimal_params, contract):
        td = render(minimal_params, contract, env={"main": []})
        assert "secrets" not in td["containerDefinitions"][0]

    def test_env_is_container_scoped(self, minimal_params, contract):
        # Env discovered for a container not in this task is simply unused.
        td = render(minimal_params, contract, env={"other": ENV_ROUTER})
        assert "secrets" not in td["containerDefinitions"][0]


class TestLogConfiguration:
    def test_override_is_verbatim(self, minimal_params, contract):
        override = {
            "logDriver": "awsfirelens",
            "options": {"Name": "http", "Host": "listener.example.com"},
            "secretOptions": [{"name": "Shared_Key", "valueFrom": "arn:aws:ssm:::parameter/k"}],
        }
        minimal_params["ecs/log_configuration"] = json.dumps(override)
        td = render(minimal_params, contract)
        # Verbatim: no awslogs-region or stream prefix injected.
        assert td["containerDefinitions"][0]["logConfiguration"] == override

    def test_override_must_have_log_driver(self, minimal_params, contract):
        minimal_params["ecs/log_configuration"] = json.dumps({"options": {}})
        with pytest.raises(RenderError, match="logDriver"):
            render(minimal_params, contract)


class TestHealthCheck:
    def test_transform_and_defaults(self, minimal_params, contract):
        minimal_params["ecs/health_check"] = json.dumps(
            {"command": ["curl", "-f", "http://localhost/health"], "interval": 10}
        )
        td = render(minimal_params, contract)
        assert td["containerDefinitions"][0]["healthCheck"] == {
            "command": ["CMD-SHELL", "curl -f http://localhost/health"],
            "interval": 10,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 60,
        }

    def test_command_must_be_a_list(self, minimal_params, contract):
        minimal_params["ecs/health_check"] = json.dumps({"command": "curl"})
        with pytest.raises(RenderError, match="health_check"):
            render(minimal_params, contract)


class TestSidecars:
    def test_sidecars_appended_verbatim_with_env_injection(self, full_params, contract):
        env = {"main": ENV_MAIN, "log-router": ENV_ROUTER}
        td = render(full_params, contract, env=env)
        assert [c["name"] for c in td["containerDefinitions"]] == ["main", "log-router"]
        router = td["containerDefinitions"][1]
        # Existing raw secrets kept, discovered env appended after them.
        assert router["secrets"] == [
            {"name": "PRESET", "valueFrom": "arn:aws:ssm:us-east-2:123456789012:parameter/preset"},
            *ENV_ROUTER,
        ]
        assert router["firelensConfiguration"] == {"type": "fluentbit"}

    def test_sidecar_without_env_untouched(self, full_params, contract):
        td = render(full_params, contract, env={"main": ENV_MAIN})
        router = td["containerDefinitions"][1]
        assert router["secrets"] == [
            {"name": "PRESET", "valueFrom": "arn:aws:ssm:us-east-2:123456789012:parameter/preset"}
        ]

    def test_sidecar_needs_a_name(self, minimal_params, contract):
        minimal_params["ecs/sidecars"] = json.dumps([{"image": "x"}])
        with pytest.raises(RenderError, match="name"):
            render(minimal_params, contract)

    def test_duplicate_container_names_rejected(self, minimal_params, contract):
        minimal_params["ecs/sidecars"] = json.dumps([{"name": "main", "image": "x"}])
        with pytest.raises(RenderError, match="duplicate"):
            render(minimal_params, contract)

    def test_container_names_lists_primary_then_sidecars(self, full_params, contract):
        assert container_names(full_params, contract) == ["main", "log-router"]


class TestFull:
    def test_full_render(self, full_params, contract):
        td = render(full_params, contract, env={"main": ENV_MAIN, "log-router": ENV_ROUTER})
        assert td["cpu"] == "1024"
        assert td["memory"] == "2048"
        assert td["runtimePlatform"]["cpuArchitecture"] == "X86_64"
        assert td["ephemeralStorage"] == {"sizeInGiB": 50}
        main = td["containerDefinitions"][0]
        assert main["command"] == ["bundle", "exec", "puma"]
        assert main["ulimits"] == [{"name": "nofile", "softLimit": 65536, "hardLimit": 65536}]
        assert main["stopTimeout"] == 60
        assert main["linuxParameters"] == {"initProcessEnabled": False}
        assert main["portMappings"] == [
            {"containerPort": 4000, "protocol": "tcp", "name": "main", "appProtocol": "http2"}
        ]
        assert main["secrets"] == ENV_MAIN

    def test_renderer_is_pure(self, full_params, contract):
        env = {"main": list(ENV_MAIN), "log-router": list(ENV_ROUTER)}
        before = json.dumps({"params": full_params, "env": env}, sort_keys=True)
        first = render(full_params, contract, env=env)
        second = render(full_params, contract, env=env)
        assert first == second
        assert json.dumps({"params": full_params, "env": env}, sort_keys=True) == before
        # Mutating the output must not leak into a later render (deep copies).
        first["containerDefinitions"][1]["secrets"].append({"name": "X", "valueFrom": "y"})
        third = render(full_params, contract, env=env)
        assert third == second
