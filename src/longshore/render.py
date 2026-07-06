"""Pure renderer: SSM parameter dict → ECS task-definition dict.

No AWS calls happen here. Inputs are the raw config params (from
``ssm.read_config``), the per-container env entries (from
``ssm.discover_env_for``), and the contract, which drives validation:
required keys, value types, enums, and defaults.
"""

from __future__ import annotations

import copy
from typing import Any

from .contract import Contract, ContractError

# Renderer-set fields that come from arguments, not SSM — kept in sync with
# the contract's fixed_fields section (pinned by tests/test_contract.py).
FIXED_TASK_FIELDS = {
    "requiresCompatibilities": ["FARGATE"],
    "networkMode": "awsvpc",
}
OPERATING_SYSTEM_FAMILY = "LINUX"

HEALTH_CHECK_DEFAULTS = {"interval": 30, "timeout": 5, "retries": 3, "start_period": 60}


class RenderError(Exception):
    """The SSM params violate the contract, or reference broken sidecar defs."""


def typed_config(params: dict[str, str], contract: Contract) -> dict[str, Any]:
    """Validate `params` against the contract and parse task-definition keys.

    Returns a dict of contract key → typed value: every task-definition key
    present in `params` parsed per its declared type, plus contract defaults
    for absent keys that have one. Non-contract keys are ignored (forward
    compatibility).
    """
    if not params:
        raise RenderError("no SSM parameters found under the service prefix")
    missing = sorted(
        k.key for k in contract.task_definition_keys if k.required and k.key not in params
    )
    if missing:
        raise RenderError(f"missing required SSM parameters: {', '.join(missing)}")

    config: dict[str, Any] = {}
    for key in contract.task_definition_keys:
        if key.key in params:
            config[key.key] = key.parse(params[key.key])
        elif key.default is not None:
            config[key.key] = key.default
    return config


def container_names(params: dict[str, str], contract: Contract) -> list[str]:
    """Every container in the task: the primary first, then declared sidecars."""
    config = typed_config(params, contract)
    return [config["ecs/container_name"]] + [s["name"] for s in _sidecars(config)]


def render_task_definition(
    service: str,
    image: str,
    params: dict[str, str],
    env_by_container: dict[str, list[dict[str, str]]],
    region: str,
    contract: Contract,
) -> dict[str, Any]:
    """Render the complete RegisterTaskDefinition payload."""
    config = typed_config(params, contract)
    primary_name = config["ecs/container_name"]

    primary: dict[str, Any] = {
        "name": primary_name,
        "image": image,
        "essential": True,
        "stopTimeout": config["ecs/stop_timeout"],
        "linuxParameters": {"initProcessEnabled": config["ecs/init_process"]},
        "logConfiguration": _log_configuration(config, primary_name, region),
    }

    port_mapping = _port_mapping(config, primary_name)
    if port_mapping is not None:
        primary["portMappings"] = [port_mapping]

    env = env_by_container.get(primary_name)
    if env:
        primary["secrets"] = list(env)

    if "ecs/command" in config:
        primary["command"] = config["ecs/command"]
    if "ecs/ulimits" in config:
        primary["ulimits"] = config["ecs/ulimits"]
    if "ecs/health_check" in config:
        primary["healthCheck"] = _health_check(config["ecs/health_check"])

    containers = [primary] + _sidecar_definitions(config, primary_name, env_by_container)

    task_def: dict[str, Any] = {
        "family": service,
        **FIXED_TASK_FIELDS,
        "cpu": str(config["ecs/cpu"]),
        "memory": str(config["ecs/memory"]),
        "executionRoleArn": config["ecs/execution_role_arn"],
        "taskRoleArn": config["ecs/task_role_arn"],
        "runtimePlatform": {
            "cpuArchitecture": config["ecs/cpu_architecture"],
            "operatingSystemFamily": OPERATING_SYSTEM_FAMILY,
        },
        "containerDefinitions": containers,
    }
    if "ecs/ephemeral_storage" in config:
        task_def["ephemeralStorage"] = {"sizeInGiB": config["ecs/ephemeral_storage"]}
    return task_def


def _log_configuration(config: dict[str, Any], primary_name: str, region: str) -> dict[str, Any]:
    # An explicit ecs/log_configuration replaces the awslogs default verbatim —
    # the renderer injects nothing into it, not even awslogs-region.
    if "ecs/log_configuration" in config:
        override = config["ecs/log_configuration"]
        if not isinstance(override, dict) or "logDriver" not in override:
            raise RenderError("ecs/log_configuration must be an object with a logDriver")
        return override
    return {
        "logDriver": "awslogs",
        "options": {
            "awslogs-group": config["ecs/log_group_name"],
            "awslogs-region": region,
            "awslogs-stream-prefix": primary_name,
        },
    }


def _port_mapping(config: dict[str, Any], primary_name: str) -> dict[str, Any] | None:
    if "ecs/container_port" not in config:
        return None
    mapping: dict[str, Any] = {"containerPort": config["ecs/container_port"], "protocol": "tcp"}
    if "ecs/service_connect_namespace_arn" in config:
        mapping["name"] = primary_name
        mapping["appProtocol"] = config.get("ecs/service_connect_app_protocol", "http")
    return mapping


def _health_check(raw: Any) -> dict[str, Any]:
    # Producers write snake_case; apply the contract's defaults and
    # transform to the ECS shape.
    if not isinstance(raw, dict) or not isinstance(raw.get("command"), list):
        raise RenderError("ecs/health_check must be an object with a command list")
    merged = {**HEALTH_CHECK_DEFAULTS, **raw}
    return {
        "command": ["CMD-SHELL", " ".join(merged["command"])],
        "interval": merged["interval"],
        "timeout": merged["timeout"],
        "retries": merged["retries"],
        "startPeriod": merged["start_period"],
    }


def _sidecars(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("ecs/sidecars", [])
    if not isinstance(raw, list) or not all(isinstance(s, dict) for s in raw):
        raise RenderError("ecs/sidecars must be a JSON list of container definitions")
    for sidecar in raw:
        if not isinstance(sidecar.get("name"), str) or not sidecar["name"]:
            raise RenderError("every sidecar definition needs a non-empty name")
    return raw


def _sidecar_definitions(
    config: dict[str, Any],
    primary_name: str,
    env_by_container: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    definitions = []
    seen = {primary_name}
    for raw in _sidecars(config):
        sidecar = copy.deepcopy(raw)
        if sidecar["name"] in seen:
            raise RenderError(f"duplicate container name {sidecar['name']!r}")
        seen.add(sidecar["name"])
        # Env injection is the one exception to "appended verbatim": each
        # sidecar's discovered env is appended to any secrets it already has.
        env = env_by_container.get(sidecar["name"])
        if env:
            sidecar["secrets"] = list(sidecar.get("secrets", [])) + list(env)
        definitions.append(sidecar)
    return definitions


def compliance_report(params: dict[str, str], contract: Contract) -> tuple[list[str], list[str]]:
    """Check every contract key against a prefix's live parameters.

    Returns (issues, ignored): `issues` are contract violations — missing
    required keys and values that fail their declared type or enum, across
    both the deploy and task_definition sections; `ignored` are parameter
    keys present under the prefix that are not part of the contract (the
    tool skips them, but surfacing them catches typos)."""
    issues: list[str] = []
    for key in contract.keys:
        if key.key not in params:
            if key.required:
                issues.append(f"missing required parameter: {key.key}")
            continue
        try:
            key.parse(params[key.key])
        except ContractError as exc:
            issues.append(str(exc))
    known = {k.key for k in contract.keys}
    ignored = sorted(k for k in params if k not in known)
    return issues, ignored


def parse_engine_config(params: dict[str, str], contract: Contract) -> dict[str, Any]:
    """Typed values for the engine-read keys (cluster, run-task networking)."""
    config: dict[str, Any] = {}
    missing = []
    for key in contract.deploy_keys:
        if key.key in params:
            config[key.key] = key.parse(params[key.key])
        elif key.required:
            missing.append(key.key)
    if missing:
        raise RenderError(f"missing required SSM parameters: {', '.join(sorted(missing))}")
    return config


def contract_keys_handled() -> frozenset[str]:
    """The task-definition keys this renderer implements — pinned against
    the contract by tests so spec and code cannot drift silently."""
    return frozenset(
        {
            "ecs/execution_role_arn",
            "ecs/task_role_arn",
            "ecs/cpu",
            "ecs/memory",
            "ecs/cpu_architecture",
            "ecs/ephemeral_storage",
            "ecs/container_name",
            "ecs/container_port",
            "ecs/log_group_name",
            "ecs/log_configuration",
            "ecs/stop_timeout",
            "ecs/init_process",
            "ecs/command",
            "ecs/ulimits",
            "ecs/health_check",
            "ecs/sidecars",
            "ecs/service_connect_namespace_arn",
            "ecs/service_connect_app_protocol",
        }
    )
