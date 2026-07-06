"""Engine coverage: register/repoint call shape, rollout watcher paths,
diff canonicalization, and the run helpers — all against stubbed clients."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from longshore.engine import (
    DeployError,
    canonicalize,
    current_task_definition,
    diff_task_definitions,
    register_task_definition,
    repoint_service,
    run_task,
    wait_task_stopped,
    watch_rollout,
)

TD_ARN = "arn:aws:ecs:us-east-2:123456789012:task-definition/myapp-staging:7"
CLUSTER = "arn:aws:ecs:us-east-2:123456789012:cluster/staging"
SERVICE = "myapp-staging"


def access_denied(operation):
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, operation)


class FakeEcs:
    """Records every call; responses are queued per operation."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def _respond(self, operation, kwargs):
        self.calls.append((operation, kwargs))
        queued = self.responses.get(operation)
        if callable(queued):
            return queued(kwargs)
        if isinstance(queued, list):
            response = queued.pop(0) if len(queued) > 1 else queued[0]
            if isinstance(response, Exception):
                raise response
            return response
        return queued or {}

    def __getattr__(self, name):
        return lambda **kwargs: self._respond(name, kwargs)


def service_response(deployments, events=None):
    return {"services": [{"deployments": deployments, "events": events or []}]}


def deployment(state, running=1, desired=1, failed=0, arn=TD_ARN, reason=""):
    return {
        "taskDefinition": arn,
        "status": "PRIMARY",
        "rolloutState": state,
        "rolloutStateReason": reason,
        "runningCount": running,
        "desiredCount": desired,
        "failedTasks": failed,
    }


class TestRegisterAndRepoint:
    def test_register_returns_arn(self):
        ecs = FakeEcs(
            {"register_task_definition": {"taskDefinition": {"taskDefinitionArn": TD_ARN}}}
        )
        task_def = {"family": SERVICE, "containerDefinitions": []}
        assert register_task_definition(ecs, task_def) == TD_ARN
        assert ecs.calls == [("register_task_definition", task_def)]

    def test_repoint_passes_only_task_definition(self):
        """The structural guarantee: update_service must never carry desired
        count, networking, or load-balancer arguments."""
        ecs = FakeEcs()
        repoint_service(ecs, CLUSTER, SERVICE, TD_ARN)
        operation, kwargs = ecs.calls[0]
        assert operation == "update_service"
        assert set(kwargs) == {"cluster", "service", "taskDefinition"}
        assert kwargs == {"cluster": CLUSTER, "service": SERVICE, "taskDefinition": TD_ARN}


class TestWatchRollout:
    def watch(self, ecs, **kwargs):
        output = []
        result = watch_rollout(
            ecs,
            CLUSTER,
            SERVICE,
            TD_ARN,
            out=output.append,
            sleep=lambda _s: None,
            **kwargs,
        )
        return result, "\n".join(output)

    def test_completed(self):
        ecs = FakeEcs(
            {
                "describe_services": [
                    service_response([deployment("IN_PROGRESS", running=0)]),
                    service_response([deployment("COMPLETED", running=2, desired=2)]),
                ]
            }
        )
        result, output = self.watch(ecs)
        assert result is True
        assert "rollout COMPLETED" in output

    def test_failed_reports_events_and_stopped_tasks(self):
        stale_event = {"id": "old", "message": "(service myapp) has reached a steady state."}
        fresh_event = {
            "id": "new",
            "message": "(service myapp) is unable to consistently start tasks.",
        }
        ecs = FakeEcs(
            {
                "describe_services": [
                    service_response([deployment("IN_PROGRESS")], events=[stale_event]),
                    service_response(
                        [deployment("FAILED", failed=3, reason="tasks failed to start")],
                        events=[fresh_event, stale_event],
                    ),
                ],
                "list_tasks": {"taskArns": [f"{CLUSTER}/task-1"]},
                "describe_tasks": {
                    "tasks": [
                        {
                            "taskArn": "arn:aws:ecs:::task/staging/task-1",
                            "taskDefinitionArn": TD_ARN,
                            "stoppedReason": "Essential container in task exited",
                            "containers": [{"name": "main", "exitCode": 1}],
                        }
                    ]
                },
            }
        )
        result, output = self.watch(ecs)
        assert result is False
        assert "tasks failed to start" in output
        assert "unable to consistently start tasks" in output
        assert "steady state" not in output  # pre-watch events are not replayed
        assert "Essential container in task exited" in output
        assert "exit code 1" in output
        assert "circuit breaker" in output

    def test_failed_degrades_without_task_permissions(self):
        """A role without ListTasks/DescribeTasks — point at the console
        instead of crashing."""
        ecs = FakeEcs(
            {
                "describe_services": [
                    service_response([deployment("FAILED", reason="circuit breaker triggered")])
                ],
                "list_tasks": [access_denied("ListTasks"), access_denied("ListTasks")],
            }
        )
        result, output = self.watch(ecs)
        assert result is False
        assert "check the ECS console" in output

    def test_timeout(self):
        ecs = FakeEcs({"describe_services": [service_response([deployment("IN_PROGRESS")])]})
        clock = iter(range(0, 10_000, 100))
        result = watch_rollout(
            ecs,
            CLUSTER,
            SERVICE,
            TD_ARN,
            wait_minutes=1,
            out=lambda _line: None,
            sleep=lambda _s: None,
            monotonic=lambda: float(next(clock)),
        )
        assert result is False

    def test_superseded_deployment(self):
        other = deployment("IN_PROGRESS", arn=TD_ARN.replace(":7", ":8"))
        ecs = FakeEcs({"describe_services": [service_response([other])]})
        result, output = self.watch(ecs)
        assert result is False
        assert "superseded" in output

    def test_missing_service_raises(self):
        ecs = FakeEcs({"describe_services": {"services": [], "failures": [{"reason": "MISSING"}]}})
        with pytest.raises(DeployError, match="MISSING"):
            self.watch(ecs)


class TestDiff:
    def rendered(self):
        return {
            "family": SERVICE,
            "requiresCompatibilities": ["FARGATE"],
            "networkMode": "awsvpc",
            "cpu": "256",
            "memory": "512",
            "executionRoleArn": "arn:exec",
            "taskRoleArn": "arn:task",
            "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
            "containerDefinitions": [
                {
                    "name": "main",
                    "image": "repo:v1",
                    "essential": True,
                    "portMappings": [{"containerPort": 4000, "protocol": "tcp"}],
                }
            ],
        }

    def registered(self):
        # What DescribeTaskDefinition returns for the same definition: ECS
        # materializes defaults the renderer never writes.
        td = self.rendered()
        container = td["containerDefinitions"][0]
        container.update(cpu=0, mountPoints=[], volumesFrom=[], environment=[], systemControls=[])
        container["portMappings"] = [{"containerPort": 4000, "hostPort": 4000, "protocol": "tcp"}]
        return td

    def test_equal_after_canonicalization(self):
        assert diff_task_definitions(self.registered(), self.rendered()) == ""

    def test_image_change_shows_up(self):
        rendered = self.rendered()
        rendered["containerDefinitions"][0]["image"] = "repo:v2"
        diff = diff_task_definitions(self.registered(), rendered)
        assert '-      "image": "repo:v1"' in diff
        assert '+      "image": "repo:v2"' in diff

    def test_current_task_definition_strips_metadata(self):
        described = {
            **self.rendered(),
            "taskDefinitionArn": TD_ARN,
            "revision": 7,
            "status": "ACTIVE",
            "registeredAt": "2026-07-05T00:00:00Z",
            "compatibilities": ["EC2", "FARGATE"],
            "requiresAttributes": [{"name": "ecs.capability.execution-role-ecr-pull"}],
        }
        ecs = FakeEcs(
            {
                "describe_services": {"services": [{"taskDefinition": TD_ARN}]},
                "describe_task_definition": {"taskDefinition": described},
            }
        )
        assert current_task_definition(ecs, CLUSTER, SERVICE) == self.rendered()

    def test_canonicalize_keeps_meaningful_zeroes(self):
        assert canonicalize({"stopTimeout": 0}) == {"stopTimeout": 0}
        assert canonicalize({"cpu": 0}) == {}
        assert canonicalize({"hostPort": 8080, "containerPort": 4000}) == {
            "hostPort": 8080,
            "containerPort": 4000,
        }


class TestRunTask:
    def test_run_task_shape_and_arn(self):
        ecs = FakeEcs({"run_task": {"tasks": [{"taskArn": "arn:task/abc"}], "failures": []}})
        arn = run_task(ecs, CLUSTER, TD_ARN, ["subnet-a"], ["sg-1"], "main", command=["migrate"])
        assert arn == "arn:task/abc"
        operation, kwargs = ecs.calls[0]
        assert operation == "run_task"
        assert kwargs["launchType"] == "FARGATE"
        assert kwargs["networkConfiguration"]["awsvpcConfiguration"] == {
            "subnets": ["subnet-a"],
            "securityGroups": ["sg-1"],
            "assignPublicIp": "DISABLED",
        }
        assert kwargs["overrides"] == {
            "containerOverrides": [{"name": "main", "command": ["migrate"]}]
        }

    def test_run_task_no_command_no_overrides(self):
        ecs = FakeEcs({"run_task": {"tasks": [{"taskArn": "arn:task/abc"}]}})
        run_task(ecs, CLUSTER, TD_ARN, ["subnet-a"], ["sg-1"], "main")
        assert "overrides" not in ecs.calls[0][1]

    def test_run_task_failure_raises(self):
        ecs = FakeEcs({"run_task": {"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]}})
        with pytest.raises(DeployError, match="RESOURCE:MEMORY"):
            run_task(ecs, CLUSTER, TD_ARN, ["subnet-a"], ["sg-1"], "main")

    def test_wait_task_stopped_returns_exit_code(self):
        ecs = FakeEcs(
            {
                "describe_tasks": [
                    {"tasks": [{"lastStatus": "RUNNING"}]},
                    {
                        "tasks": [
                            {
                                "lastStatus": "STOPPED",
                                "stoppedReason": "Essential container exited",
                                "containers": [{"name": "main", "exitCode": 3}],
                            }
                        ]
                    },
                ]
            }
        )
        code = wait_task_stopped(
            ecs, CLUSTER, "arn:task/abc", "main", out=lambda _l: None, sleep=lambda _s: None
        )
        assert code == 3

    def test_wait_task_stopped_no_exit_code_is_failure(self):
        ecs = FakeEcs(
            {
                "describe_tasks": {
                    "tasks": [{"lastStatus": "STOPPED", "containers": [{"name": "main"}]}]
                }
            }
        )
        code = wait_task_stopped(
            ecs, CLUSTER, "arn:task/abc", "main", out=lambda _l: None, sleep=lambda _s: None
        )
        assert code == 1
