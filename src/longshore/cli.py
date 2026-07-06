"""The longshore CLI: deploy / render / diff / validate / run / exec.

Same tool, same commands, in CI or locally. CI is expected to use only
validate/render/diff (config gates) and deploy — the CI deploy role is
deliberately not granted RunTask/ExecuteCommand; run and exec are local
operator verbs.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import __version__
from .contract import Contract, ContractError, load_contract
from .engine import (
    DEFAULT_WAIT_MINUTES,
    DeployError,
    current_task_definition,
    describe_service,
    diff_task_definitions,
    register_task_definition,
    repoint_service,
    run_task,
    wait_task_stopped,
    watch_rollout,
)
from .render import (
    RenderError,
    compliance_report,
    container_names,
    parse_engine_config,
    render_task_definition,
)
from .ssm import discover_env_for, read_config

RENDER_IMAGE_PLACEHOLDER = "<image>"


class CliError(Exception):
    """A usage or environment problem surfaced to the user without a traceback."""


@dataclass
class Context:
    """Everything a verb needs: clients, contract, and the service's SSM state."""

    service: str
    region: str
    contract: Contract
    prefix: str
    params: dict[str, str]
    session: Any

    _clients: dict[str, Any] | None = None

    def client(self, name: str) -> Any:
        if self._clients is None:
            self._clients = {}
        if name not in self._clients:
            self._clients[name] = self.session.client(name)
        return self._clients[name]

    @property
    def cluster(self) -> str:
        engine = parse_engine_config(self.params, self.contract)
        return str(engine["ecs/cluster_arn"])

    def env_by_container(self) -> dict[str, list[dict[str, str]]]:
        names = container_names(self.params, self.contract)
        return discover_env_for(self.client("ssm"), self.prefix, names)

    def render(self, image: str) -> dict[str, Any]:
        return render_task_definition(
            self.service, image, self.params, self.env_by_container(), self.region, self.contract
        )


def build_context(args: argparse.Namespace) -> Context:
    session = boto3.session.Session(
        region_name=args.region or None, profile_name=args.profile or None
    )
    region = session.region_name
    if not region:
        raise CliError("no AWS region configured; pass --region or set AWS_REGION/a profile region")
    contract = load_contract()
    prefix = contract.prefix_for(args.service)
    params = read_config(session.client("ssm"), prefix)
    if not params:
        raise CliError(
            f"no SSM parameters found under {prefix}/ — is the service name right, "
            "and has the Service layer been applied?"
        )
    ctx = Context(
        service=args.service,
        region=region,
        contract=contract,
        prefix=prefix,
        params=params,
        session=session,
    )
    ctx._clients = {"ssm": session.client("ssm")}
    return ctx


# --------------------------------------------------------------------------
# verbs
# --------------------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    task_def = ctx.render(args.image or RENDER_IMAGE_PLACEHOLDER)
    rendered = json.dumps(task_def, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
        print(f"task definition written to {args.output}")
    else:
        print(rendered)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    ecs = ctx.client("ecs")
    current = current_task_definition(ecs, ctx.cluster, ctx.service)
    image = args.image or _primary_image(current, ctx)
    rendered = ctx.render(image)
    diff = diff_task_definitions(current, rendered)
    if diff:
        print(diff)
    else:
        print("no changes: rendered task definition matches the registered revision")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Check a service prefix for compliance with the bundled contract."""
    ctx = build_context(args)
    print(f"contract v{ctx.contract.version}, prefix {ctx.prefix}")

    issues, ignored = compliance_report(ctx.params, ctx.contract)
    for key in ignored:
        print(f"note: {key} is not part of the deploy contract (ignored)")

    if not issues:
        # Key-level checks passed; exercise the full render to catch
        # structural problems (sidecar names, health-check shape, …).
        try:
            names = container_names(ctx.params, ctx.contract)
            env = ctx.env_by_container()
            for name in names:
                print(f"container {name}: {len(env.get(name, []))} env param(s)")
            ctx.render(RENDER_IMAGE_PLACEHOLDER)
        except (RenderError, ContractError) as exc:
            issues.append(str(exc))

    if issues:
        for issue in issues:
            print(f"violation: {issue}")
        print(f"{ctx.service} is NOT compliant ({len(issues)} violation(s))")
        return 1
    print(f"{ctx.service} is compliant")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    ecs = ctx.client("ecs")
    task_def = ctx.render(args.image)
    arn = register_task_definition(ecs, task_def)
    print(f"registered {arn}")
    repoint_service(ecs, ctx.cluster, ctx.service, arn)
    print(f"service {ctx.service} repointed")
    if args.wait_minutes <= 0:
        print("wait disabled (--wait-minutes 0); not watching the rollout")
        return 0
    print("watching rollout")
    ok = watch_rollout(ecs, ctx.cluster, ctx.service, arn, wait_minutes=args.wait_minutes)
    print("deploy succeeded" if ok else "deploy failed")
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    ecs = ctx.client("ecs")
    engine_config = parse_engine_config(ctx.params, ctx.contract)
    subnets = engine_config.get("ecs/subnets")
    security_groups = engine_config.get("ecs/security_group_ids")
    if not subnets or not security_groups:
        raise CliError(
            "ecs/subnets and ecs/security_group_ids must be present in SSM to run a task"
        )
    primary = container_names(ctx.params, ctx.contract)[0]

    if args.image:
        arn = register_task_definition(ecs, ctx.render(args.image))
        print(f"registered {arn}")
    else:
        arn = _current_task_definition_arn(ecs, ctx.cluster, ctx.service)

    command = _parse_command(args.command) if args.command else None
    task_arn = run_task(ecs, ctx.cluster, arn, subnets, security_groups, primary, command=command)
    task_id = task_arn.rsplit("/", 1)[-1]
    print(f"started task {task_id}")
    log_group = ctx.params.get("ecs/log_group_name")
    if log_group:
        print(f"logs: {log_group} stream {primary}/{primary}/{task_id}")
    return wait_task_stopped(ecs, ctx.cluster, task_arn, primary, wait_minutes=args.wait_minutes)


def _current_task_definition_arn(ecs: Any, cluster: str, service: str) -> str:
    arn = describe_service(ecs, cluster, service).get("taskDefinition")
    if not arn:
        raise DeployError(f"service {service!r} has no task definition to run")
    return str(arn)


def cmd_exec(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    ecs = ctx.client("ecs")
    container = args.container or container_names(ctx.params, ctx.contract)[0]
    task = args.task
    if not task:
        arns = ecs.list_tasks(
            cluster=ctx.cluster, serviceName=ctx.service, desiredStatus="RUNNING"
        ).get("taskArns", [])
        if not arns:
            raise CliError(f"no running tasks for service {ctx.service}")
        task = arns[0].rsplit("/", 1)[-1]
    aws = shutil.which("aws")
    if not aws:
        raise CliError("the aws CLI is required for exec (plus the session-manager-plugin)")
    argv = [
        aws,
        "ecs",
        "execute-command",
        "--region",
        ctx.region,
        "--cluster",
        ctx.cluster,
        "--task",
        task,
        "--container",
        container,
        "--interactive",
        "--command",
        args.command,
    ]
    if args.profile:
        argv[1:1] = ["--profile", args.profile]
    print(f"execing into task {task} container {container}")
    os.execv(aws, argv)
    return 127  # pragma: no cover - execv does not return


def _primary_image(current: dict[str, Any], ctx: Context) -> str:
    primary = container_names(ctx.params, ctx.contract)[0]
    for container in current.get("containerDefinitions", []):
        if container.get("name") == primary:
            return str(container.get("image", RENDER_IMAGE_PLACEHOLDER))
    return RENDER_IMAGE_PLACEHOLDER


def _parse_command(raw: str) -> list[str]:
    """Accept a JSON list ('["php", "artisan", "migrate"]') or a shell string."""
    if raw.lstrip().startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not all(isinstance(i, str) for i in parsed):
            raise CliError("--command JSON must be a list of strings")
        return parsed
    return shlex.split(raw)


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="longshore",
        description="SSM-contract-driven ECS deployment tool",
    )
    parser.add_argument("--version", action="version", version=f"longshore {__version__}")
    subparsers = parser.add_subparsers(dest="verb", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--service", required=True, help="service name (= SSM prefix and family)")
        sub.add_argument("--region", default=None, help="AWS region (default: environment/profile)")
        sub.add_argument("--profile", default=None, help="AWS profile (local use)")

    deploy = subparsers.add_parser("deploy", help="render → register → repoint → observe")
    common(deploy)
    deploy.add_argument("--image", required=True, help="full image URL with tag")
    deploy.add_argument(
        "--wait-minutes",
        type=float,
        default=DEFAULT_WAIT_MINUTES,
        help=f"rollout watch timeout (default {DEFAULT_WAIT_MINUTES:g}; 0 disables the watch)",
    )
    deploy.set_defaults(func=cmd_deploy)

    render = subparsers.add_parser("render", help="render the task-def JSON without deploying")
    common(render)
    render.add_argument(
        "--image", default=None, help=f"image URL (default {RENDER_IMAGE_PLACEHOLDER!r})"
    )
    render.add_argument("--output", default=None, help="write JSON to a file instead of stdout")
    render.set_defaults(func=cmd_render)

    diff = subparsers.add_parser("diff", help="diff the render against the registered revision")
    common(diff)
    diff.add_argument("--image", default=None, help="image URL (default: currently deployed image)")
    diff.set_defaults(func=cmd_diff)

    validate = subparsers.add_parser(
        "validate", help="check a service prefix for contract compliance"
    )
    common(validate)
    validate.set_defaults(func=cmd_validate)

    run = subparsers.add_parser("run", help="one-off task (local operator; e.g. DB migrations)")
    common(run)
    run.add_argument(
        "--image", default=None, help="register a fresh task def with this image first"
    )
    run.add_argument("--command", default=None, help="command override: JSON list or shell string")
    run.add_argument("--wait-minutes", type=float, default=30.0, help="task wait timeout")
    run.set_defaults(func=cmd_run)

    exec_ = subparsers.add_parser(
        "exec", help="interactive shell in a running task (local operator)"
    )
    common(exec_)
    exec_.add_argument("--task", default=None, help="task ID (default: first running task)")
    exec_.add_argument("--container", default=None, help="container name (default: primary)")
    exec_.add_argument("--command", default="/bin/sh", help="command to exec (default /bin/sh)")
    exec_.set_defaults(func=cmd_exec)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except (CliError, ContractError, RenderError, DeployError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (BotoCoreError, ClientError) as exc:
        print(f"AWS error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
