# longshore

Contract-driven ECS deploys. `longshore` reads a service's configuration from
SSM Parameter Store (`/services/{name}/*`), renders a Fargate task
definition, registers it, repoints the ECS service, and observes the rollout.
It is stateless and deliberately small: your infrastructure tooling
(Terraform, CloudFormation, CDK, a script) owns the ECS service; `longshore`
owns only the task definition.

```
Producer (your IaC)                 longshore
───────────────────                 ─────────
owns the ECS service                renders task-def JSON from SSM
owns autoscaling / desired count    registers new task-def revision
owns networking, LB, roles, logs    update_service(taskDefinition=…) — nothing else
owns circuit breaker + rollback     polls rolloutState, reports events
writes /services/{name}/* (SSM)  ─► reads /services/{name}/*
```

Because `update_service` is called with **only** the `taskDefinition`
argument, the tool structurally cannot touch desired count, networking, or
load-balancer settings. Rollback is server-side via the ECS deployment
circuit breaker — `longshore` only observes and reports.

## Install

```bash
uvx longshore --help          # zero-install, always the latest release
uvx longshore==0.1.0 --help   # pinned (recommended in CI)
# or: pip install longshore
```

## Usage

```bash
# The main path (CI and local; identical behaviour)
longshore deploy --service myapp-production --image <ecr-url>:<tag> [--region us-east-2] [--wait-minutes 12]

# Config gates (read-only against AWS)
longshore validate --service myapp-production                 # contract compliance report
longshore render --service myapp-production [--image <url>] [--output task-def.json]
longshore diff   --service myapp-production [--image <url>]   # vs the registered revision

# Operator verbs (need broader IAM than the deploy path; see below)
longshore run  --service myapp-production --command 'php artisan migrate --force'
longshore exec --service myapp-production                     # needs the session-manager-plugin
```

Cluster, roles, networking, and log-group coordinates all come from SSM —
there is no per-service config file. Adding a service or a secret requires no
change here.

### Exit codes

- `0` — success (`deploy`: rollout COMPLETED; `run`: container exited 0).
- non-zero — validation error, rollout FAILED (circuit breaker triggered;
  rollback is already underway server-side), watch timeout, or the one-off
  task's exit code.

## The contract

The SSM interface — which parameter keys the tool understands relative to
`/services/{name}/`, their parse semantics, and when they are required — is
defined by the versioned contract bundled with the package:
[`src/longshore/data/services.yaml`](src/longshore/data/services.yaml). It is
the single source of truth and is not user-configurable; any producer that
writes compliant parameters works with this tool unmodified.

`longshore validate --service <name>` reports a live prefix's compliance:
missing required keys, type/enum violations, structural problems (e.g. a
sidecar without a name), per-container env counts, and any keys under the
prefix the tool ignores.

Highlights of the interface:

- **Env is per-container and secrets-only.** Every parameter under
  `containers/{container}/env/` (String or SecureString) becomes a
  `valueFrom` secret on that container — values are never read by the tool,
  only ARNs; ECS resolves them at task start via the execution role.
- **Sidecars** are raw container definitions published at `ecs/sidecars` with
  producer-pinned images; `--image` only ever targets the primary container.
- **FireLens** needs no special support: a log-router sidecar plus an
  `ecs/log_configuration` override (used verbatim) switches the primary
  container to `awsfirelens`.

Interface changes bump the contract `version` and ship as new releases.

## GitHub Action

The repo doubles as a composite action pinned to the same version as the
code:

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write   # OIDC
      contents: read
    steps:
      - uses: telasystems/longshore@v0
        with:
          service-name: myapp-production
          image: ${{ needs.build.outputs.image }}
          role-arn: ${{ vars.DEPLOY_ROLE_ARN }}
```

## IAM for the deploy path

A CI deploy role needs only: `ssm:GetParameter`/`GetParametersByPath` on the
service prefix, `ecs:RegisterTaskDefinition` (+ `DescribeTaskDefinition`,
`TagResource`), `ecs:UpdateService`/`DescribeServices` on the one service,
`iam:PassRole` on the task's two roles, and (optional, for failure
diagnostics) cluster-scoped `ecs:ListTasks`/`DescribeTasks`. `run` and `exec`
intentionally need more (`ecs:RunTask`, `ecs:ExecuteCommand`) — keep those
off the CI role and use operator credentials.

## Development

```bash
uv sync                      # install (incl. dev deps)
uv run pytest                # tests (renderer, contract pinning, engine, CLI)
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy                  # types
```

The contract-pinning tests walk the bundled contract and assert the renderer
honours every entry (type, default, required, enum) — a contract change fails
tests until the renderer handles it, so spec and code cannot drift silently.

## License

MIT © Tela Systems
