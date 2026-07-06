"""ECS deploy engine: register → repoint → observe.

Task-definition-only by construction: ``update_service`` is called with
cluster, service, and taskDefinition — nothing else — so desired count,
networking, and load-balancer settings stay Terraform-owned and untouchable.

Rollback is server-side (the ECS deployment circuit breaker, configured by
Terraform). This engine only observes: it polls the deployment's
rolloutState, streams new service events, and reports stopped-task reasons
on failure.
"""

from __future__ import annotations

import difflib
import json
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

DEFAULT_WAIT_MINUTES = 12.0
POLL_SECONDS = 10.0
MAX_STOPPED_TASKS = 5

# Fields RegisterTaskDefinition's response adds; stripped before diffing.
REGISTRATION_METADATA = frozenset(
    {
        "taskDefinitionArn",
        "revision",
        "status",
        "requiresAttributes",
        "compatibilities",
        "registeredAt",
        "registeredBy",
        "deregisteredAt",
    }
)


class DeployError(Exception):
    """A deploy step failed before the rollout watch took over."""


def register_task_definition(ecs: Any, task_def: dict[str, Any]) -> str:
    """Register a new revision; returns its ARN."""
    response = ecs.register_task_definition(**task_def)
    arn: str = response["taskDefinition"]["taskDefinitionArn"]
    return arn


def repoint_service(ecs: Any, cluster: str, service: str, task_definition_arn: str) -> None:
    """Point the service at a new task definition — the only write to the service."""
    ecs.update_service(cluster=cluster, service=service, taskDefinition=task_definition_arn)


def describe_service(ecs: Any, cluster: str, service: str) -> dict[str, Any]:
    response = ecs.describe_services(cluster=cluster, services=[service])
    services = response.get("services", [])
    if not services:
        failures = response.get("failures", [])
        reason = failures[0].get("reason", "unknown") if failures else "unknown"
        raise DeployError(f"service {service!r} not found in cluster {cluster!r} ({reason})")
    return dict(services[0])


def current_task_definition(ecs: Any, cluster: str, service: str) -> dict[str, Any]:
    """The service's currently registered task definition, metadata stripped."""
    arn = describe_service(ecs, cluster, service).get("taskDefinition")
    if not arn:
        raise DeployError(f"service {service!r} has no task definition")
    described = ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]
    return {k: v for k, v in described.items() if k not in REGISTRATION_METADATA}


def watch_rollout(
    ecs: Any,
    cluster: str,
    service: str,
    task_definition_arn: str,
    wait_minutes: float = DEFAULT_WAIT_MINUTES,
    poll_seconds: float = POLL_SECONDS,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll the deployment for `task_definition_arn` until it completes or fails.

    Returns True on COMPLETED. On FAILED the circuit breaker has already
    triggered (and rolled back, per Terraform config); recent service events
    and stopped-task reasons are reported. On timeout the function returns
    False without interfering with the server-side rollout.
    """
    deadline = monotonic() + wait_minutes * 60
    seen_events: set[str] = set()
    last_status: tuple[Any, ...] | None = None
    first_poll = True

    while True:
        svc = describe_service(ecs, cluster, service)
        _stream_events(svc, seen_events, out, quiet=first_poll)
        first_poll = False

        deployment = next(
            (
                d
                for d in svc.get("deployments", [])
                if d.get("taskDefinition") == task_definition_arn
            ),
            None,
        )
        if deployment is None:
            out(f"deployment for {task_definition_arn} is gone — superseded by a newer deploy?")
            return False

        status = (
            deployment.get("rolloutState"),
            deployment.get("runningCount", 0),
            deployment.get("desiredCount", 0),
            deployment.get("failedTasks", 0),
        )
        if status != last_status:
            out(
                f"rollout {status[0]}: running {status[1]}/{status[2]}"
                + (f", failed tasks {status[3]}" if status[3] else "")
            )
            last_status = status

        state = deployment.get("rolloutState")
        if state == "COMPLETED":
            return True
        if state == "FAILED":
            reason = deployment.get("rolloutStateReason", "no reason given")
            out(f"rollout FAILED: {reason}")
            out("the deployment circuit breaker has triggered; rollback is server-side")
            for line in _stopped_task_reasons(ecs, cluster, service, task_definition_arn):
                out(line)
            return False

        if monotonic() >= deadline:
            out(
                f"timed out after {wait_minutes:g} minutes waiting for the rollout; "
                "the deployment continues server-side (circuit breaker still applies)"
            )
            return False
        sleep(poll_seconds)


def _stream_events(
    svc: dict[str, Any], seen: set[str], out: Callable[[str], None], quiet: bool
) -> None:
    """Print service events not yet seen. Events present before the watch
    started (the first poll) are marked seen without printing."""
    fresh = [e for e in svc.get("events", []) if e.get("id") and e["id"] not in seen]
    for event in fresh:
        seen.add(event["id"])
    if quiet:
        return
    for event in reversed(fresh):  # the API returns newest-first
        out(f"event: {event.get('message', '')}")


def _stopped_task_reasons(
    ecs: Any, cluster: str, service: str, task_definition_arn: str
) -> list[str]:
    """Best-effort stopped-task diagnostics. Needs ecs:ListTasks/DescribeTasks
    (the deploy role grants them cluster-scoped); with a role that lacks them,
    degrade to a pointer instead of failing."""
    try:
        arns = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="STOPPED").get(
            "taskArns", []
        )[:MAX_STOPPED_TASKS]
        if not arns:
            return []
        tasks = ecs.describe_tasks(cluster=cluster, tasks=arns).get("tasks", [])
    except ClientError:
        return [
            "(stopped-task details unavailable to this role — "
            "check the ECS console for stopped-task reasons)"
        ]
    lines = []
    for task in tasks:
        if task.get("taskDefinitionArn") != task_definition_arn:
            continue
        task_id = task.get("taskArn", "").rsplit("/", 1)[-1]
        lines.append(f"stopped task {task_id}: {task.get('stoppedReason', 'no reason recorded')}")
        for container in task.get("containers", []):
            detail = container.get("reason") or f"exit code {container.get('exitCode')}"
            lines.append(f"  {container.get('name')}: {detail}")
    return lines


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


def diff_task_definitions(current: dict[str, Any], rendered: dict[str, Any]) -> str:
    """Unified diff between the registered and freshly rendered task defs.

    Both sides are canonicalized first: DescribeTaskDefinition materializes
    defaults the renderer never sets (empty lists, container cpu 0, hostPort
    mirroring containerPort), which would otherwise show as noise.
    """
    lines = difflib.unified_diff(
        _dump(canonicalize(current)),
        _dump(canonicalize(rendered)),
        fromfile="registered",
        tofile="rendered",
        lineterm="",
    )
    return "\n".join(lines)


def _dump(obj: Any) -> list[str]:
    return json.dumps(obj, indent=2, sort_keys=True, default=str).splitlines()


def canonicalize(node: Any) -> Any:
    """Drop ECS-materialized defaults so semantically equal defs compare equal."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "hostPort" and value == node.get("containerPort"):
                continue
            if key in ("cpu", "memory") and value == 0:
                continue  # container-level defaults added by ECS; task-level are strings
            cleaned = canonicalize(value)
            if cleaned in (None, [], {}):
                continue
            out[key] = cleaned
        return out
    if isinstance(node, list):
        return [canonicalize(item) for item in node]
    return node


# --------------------------------------------------------------------------
# run (one-off tasks; local operator only)
# --------------------------------------------------------------------------


def run_task(
    ecs: Any,
    cluster: str,
    task_definition_arn: str,
    subnets: list[str],
    security_groups: list[str],
    container_name: str,
    command: list[str] | None = None,
) -> str:
    """RunTask with the service's Terraform-published networking; returns the task ARN."""
    kwargs: dict[str, Any] = {
        "cluster": cluster,
        "taskDefinition": task_definition_arn,
        "count": 1,
        "launchType": "FARGATE",
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": security_groups,
                "assignPublicIp": "DISABLED",
            }
        },
        "startedBy": "longshore-run",
    }
    if command is not None:
        kwargs["overrides"] = {"containerOverrides": [{"name": container_name, "command": command}]}
    response = ecs.run_task(**kwargs)
    failures = response.get("failures", [])
    if failures:
        raise DeployError(f"RunTask failed: {failures[0].get('reason', 'unknown')}")
    tasks = response.get("tasks", [])
    if not tasks:
        raise DeployError("RunTask returned no tasks and no failures")
    arn: str = tasks[0]["taskArn"]
    return arn


def wait_task_stopped(
    ecs: Any,
    cluster: str,
    task_arn: str,
    container_name: str,
    wait_minutes: float = 30.0,
    poll_seconds: float = POLL_SECONDS,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Wait for a one-off task to stop; returns the primary container's exit code."""
    deadline = monotonic() + wait_minutes * 60
    last_status = None
    while True:
        tasks = ecs.describe_tasks(cluster=cluster, tasks=[task_arn]).get("tasks", [])
        if not tasks:
            raise DeployError(f"task {task_arn} not found")
        task = tasks[0]
        status = task.get("lastStatus")
        if status != last_status:
            out(f"task {status}")
            last_status = status
        if status == "STOPPED":
            reason = task.get("stoppedReason")
            if reason:
                out(f"stopped: {reason}")
            container = next(
                (c for c in task.get("containers", []) if c.get("name") == container_name),
                None,
            )
            if container is None:
                out(f"container {container_name!r} not found on the stopped task")
                return 1
            if container.get("reason"):
                out(f"{container_name}: {container['reason']}")
            exit_code = container.get("exitCode")
            if exit_code is None:
                out(f"{container_name} recorded no exit code")
                return 1
            return int(exit_code)
        if monotonic() >= deadline:
            out(f"timed out after {wait_minutes:g} minutes; task {task_arn} is still {status}")
            return 1
        sleep(poll_seconds)
