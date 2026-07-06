"""SSM reads: service config and per-container env ARN discovery.

Two read paths, mirroring the contract:

- ``read_config`` reads everything under ``/services/{name}/`` except the
  ``containers/`` subtree, returning raw string values keyed by relative key.
- ``discover_env`` lists ``/services/{name}/containers/{container}/env``
  non-recursively and returns ``{name, valueFrom}`` entries. Values are never
  read (``WithDecryption=False``) — only ARNs; ECS resolves them at task start
  via the execution role.
"""

from __future__ import annotations

from typing import Any

ENV_SUBTREE = "containers"


def read_config(ssm: Any, prefix: str) -> dict[str, str]:
    """Read all parameters under `prefix`, excluding the containers/ subtree."""
    prefix = prefix.rstrip("/")
    skip = f"{prefix}/{ENV_SUBTREE}/"
    params: dict[str, str] = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=False):
        for p in page["Parameters"]:
            if p["Name"].startswith(skip):
                continue
            relative = p["Name"][len(prefix) :].lstrip("/")
            params[relative] = p["Value"]
    return params


def discover_env(ssm: Any, prefix: str, container: str) -> list[dict[str, str]]:
    """Discover one container's env params as sorted {name, valueFrom} entries."""
    path = f"{prefix.rstrip('/')}/{ENV_SUBTREE}/{container}/env"
    entries: list[dict[str, str]] = []
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=path, Recursive=False, WithDecryption=False):
        for p in page["Parameters"]:
            entries.append({"name": p["Name"].rsplit("/", 1)[-1], "valueFrom": p["ARN"]})
    return sorted(entries, key=lambda e: e["name"])


def discover_env_for(
    ssm: Any, prefix: str, containers: list[str]
) -> dict[str, list[dict[str, str]]]:
    """Env discovery for every container in the task, keyed by container name."""
    return {name: discover_env(ssm, prefix, name) for name in containers}
