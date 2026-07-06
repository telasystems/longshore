# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                       # install (incl. dev deps); CI uses `uv sync --frozen`
uv run pytest                 # all tests
uv run pytest tests/test_render.py::TestName::test_case   # a single test
uv run ruff check .           # lint
uv run ruff format --check .  # formatting (drop --check to apply)
uv run mypy                   # types (strict; src only)
```

CI (`.github/workflows/ci.yml`) runs the lint/format/type checks from source, but builds the wheel first and runs pytest against the *installed wheel*, not the source tree (build → test-the-artifact). `release.yml` reuses the CI workflow via `workflow_call` and publishes the exact `dist` artifact that passed tests — no rebuild on release. Python ≥3.12.

## What this tool is

`longshore` is a stateless, deliberately small CLI that deploys a container image to an existing ECS Fargate service. It reads a service's configuration from SSM Parameter Store under `/services/{name}/`, renders a Fargate task definition, registers it, repoints the service at it, and observes the rollout.

The load-bearing design constraint: **`longshore` owns only the task definition; the producer's IaC (Terraform/CDK/etc.) owns the ECS service.** `update_service` is called with *only* `cluster/service/taskDefinition`, so the tool structurally cannot touch desired count, networking, or load balancing. Rollback is server-side via the ECS deployment circuit breaker — this engine only observes and reports; it never rolls anything back itself. Preserve these invariants when editing `engine.py`.

## Architecture

The dependency flow is `cli → {contract, ssm, render, engine}`, with `render` also depending on `contract`. AWS I/O lives only in `cli`, `ssm`, and `engine`; `render` and `contract` are pure.

- **`contract.py`** — loads and validates `data/services.yaml`, the versioned definition of the SSM interface. Produces `Contract` (deploy keys, task-definition keys, env convention, fixed fields) and `ContractKey` (each key's type/required/default/enum, plus `.parse()` which turns an SSM string into a typed value). This is the source of truth for *what parameters exist and how their values parse*.
- **`data/services.yaml`** — the bundled contract itself. Bundled in the wheel (see `[tool.hatch.build.targets.wheel]`), not user-configurable. Interface changes bump `version` and ship as a new release. Adding/changing a task-definition key here **requires** a matching change in `render.py` or tests fail (see Pinning below).
- **`ssm.py`** — two read paths mirroring the contract. `read_config` reads everything under the prefix *except* the `containers/` subtree (raw strings). `discover_env`/`discover_env_for` list `containers/{container}/env` non-recursively and return `{name, valueFrom}` secret entries. Env values are never decrypted or read (`WithDecryption=False`) — only ARNs; ECS resolves them at task start via the execution role. Every env var becomes a `valueFrom` secret, never a plaintext `environment` entry.
- **`render.py`** — pure `SSM params dict → task-definition dict`. No AWS calls. `typed_config` validates required keys and parses values; `render_task_definition` builds the `RegisterTaskDefinition` payload. Only the primary container gets `--image`; sidecars carry producer-pinned images verbatim.
- **`engine.py`** — the AWS deploy verbs: `register_task_definition`, `repoint_service`, `watch_rollout` (polls `rolloutState`, streams new service events, reports stopped-task reasons on FAILED), plus `diff_task_definitions`/`canonicalize` (strips ECS-materialized defaults so semantically-equal defs compare equal) and `run_task`/`wait_task_stopped` for one-off tasks.
- **`cli.py`** — argparse entry point (`main` → `longshore` script). `build_context` assembles a `Context` (session, contract, prefix, params, lazily-cached clients). Verbs: `deploy` (render→register→repoint→observe), `render`, `diff`, `validate` (config gates, read-only), and `run`/`exec` (local operator verbs needing broader IAM — `RunTask`/`ExecuteCommand`, deliberately kept off the CI deploy role).

## The contract-pinning invariant

`data/services.yaml` and `render.py` are kept from drifting apart by `tests/test_contract.py`. `render.contract_keys_handled()` returns the exact set of task-definition keys the renderer implements, and a test asserts it equals the set declared in the contract. Other pinning tests assert every default materializes in rendered output, every required key hard-fails when absent, and every enum rejects out-of-set values.

Practical consequence: **you cannot add a task-definition key to `services.yaml` without also handling it in `render.py` (and adding it to `contract_keys_handled()`), or the suite fails** — and vice versa. `test_every_default_materializes` also keeps an explicit extractor map that must be updated when a key gains/loses a default. This is intentional: spec and code cannot silently diverge.

## Distribution

Two artifacts ship from this one repo, pinned to the same version:
- The **PyPI package** (`uvx longshore ...`). Released tag-driven: `git tag vX.Y.Z && git push --tags` → `release.yml` checks the tag matches `pyproject.toml`'s `version`, builds, publishes via PyPI trusted publishing (OIDC, no tokens), creates the GitHub release, and moves the floating major tag (`v0`/`v1`).
- The **composite GitHub Action** (`action.yml`) — configures AWS creds via OIDC then runs `longshore deploy` from the checked-out action ref, so `uses: telasystems/longshore@vX` pins code and action together.

When bumping the version, update `pyproject.toml`'s `version` before tagging (the release workflow enforces they match).

## Testing notes

Tests use `moto` for SSM and fake AWS credentials (`conftest.py` autouse fixture) so no test can reach a real account. Shared fixtures: `contract` (the loaded bundled contract), `minimal_params` (keys Terraform always writes), `full_params` (every optional renderer key exercised at once).
