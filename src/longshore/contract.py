"""Load and validate the tool's SSM interface definition.

The contract (`data/services.yaml`) is the versioned definition of the SSM
parameters the tool understands under ``/services/{name}/``: key paths
relative to the prefix, the parse semantic of each value, and when a key is
required. It ships inside the package and is not user-configurable —
``longshore validate`` checks a live prefix for compliance with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

BUNDLED_RESOURCE = "data/services.yaml"

VALUE_TYPES = frozenset({"string", "int", "bool", "json"})


class ContractError(Exception):
    """The contract is malformed, or an SSM value violates its declared type."""


@dataclass(frozen=True)
class ContractKey:
    """One parameter the tool understands, relative to the prefix."""

    key: str
    type: str
    required: bool
    default: Any = None
    enum: tuple[str, ...] | None = None

    def parse(self, raw: str) -> Any:
        """Parse the value inside an SSM String per the key's declared type."""
        value: Any
        if self.type == "string":
            value = raw
        elif self.type == "int":
            try:
                value = int(raw)
            except ValueError:
                raise ContractError(f"{self.key}: expected an integer, got {raw!r}") from None
        elif self.type == "bool":
            if raw.lower() not in ("true", "false"):
                raise ContractError(f"{self.key}: expected 'true' or 'false', got {raw!r}")
            value = raw.lower() == "true"
        elif self.type == "json":
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ContractError(f"{self.key}: invalid JSON ({exc})") from None
        else:  # pragma: no cover - loader rejects unknown types
            raise ContractError(f"{self.key}: unknown type {self.type!r}")
        if self.enum is not None and value not in self.enum:
            raise ContractError(f"{self.key}: value {value!r} not in allowed set {list(self.enum)}")
        return value


@dataclass(frozen=True)
class Contract:
    """The parsed interface: deploy coordinates, task-definition keys, the
    per-container env convention, and the renderer-set fixed fields."""

    version: int
    prefix: str
    deploy_keys: tuple[ContractKey, ...]
    task_definition_keys: tuple[ContractKey, ...]
    env: dict[str, Any]
    fixed_fields: dict[str, Any]

    def prefix_for(self, service: str) -> str:
        return self.prefix.replace("{name}", service)

    @property
    def keys(self) -> tuple[ContractKey, ...]:
        return self.deploy_keys + self.task_definition_keys

    def get(self, key: str) -> ContractKey:
        for k in self.keys:
            if k.key == key:
                return k
        raise KeyError(key)


def load_contract() -> Contract:
    """Load the bundled contract."""
    text = resources.files("longshore").joinpath(BUNDLED_RESOURCE).read_text(encoding="utf-8")
    return parse_contract(text, f"bundled ({BUNDLED_RESOURCE})")


def parse_contract(text: str, source: str) -> Contract:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError(f"contract {source} is not valid YAML: {exc}") from None
    if not isinstance(doc, dict):
        raise ContractError(f"contract {source}: expected a mapping at top level")

    version = doc.get("version")
    if version != 1:
        raise ContractError(f"contract {source}: unsupported version {version!r}")
    prefix = doc.get("prefix")
    if not isinstance(prefix, str) or "{name}" not in prefix:
        raise ContractError(f"contract {source}: prefix must contain '{{name}}'")

    deploy_keys = _parse_section(doc, "deploy", source)
    task_definition_keys = _parse_section(doc, "task_definition", source)

    names = [k.key for k in deploy_keys + task_definition_keys]
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise ContractError(f"contract {source}: duplicate keys {dupes}")

    env = doc.get("env")
    if not isinstance(env, dict) or "{container}" not in str(env.get("path", "")):
        raise ContractError(f"contract {source}: 'env' must declare a path with '{{container}}'")

    fixed_fields = doc.get("fixed_fields") or {}
    if not isinstance(fixed_fields, dict):
        raise ContractError(f"contract {source}: 'fixed_fields' must be a mapping")

    return Contract(
        version=version,
        prefix=prefix,
        deploy_keys=deploy_keys,
        task_definition_keys=task_definition_keys,
        env=env,
        fixed_fields=fixed_fields,
    )


def _parse_section(doc: dict[str, Any], section: str, source: str) -> tuple[ContractKey, ...]:
    raw = doc.get(section)
    if not isinstance(raw, list) or not raw:
        raise ContractError(f"contract {source}: '{section}' must be a non-empty list")
    return tuple(_parse_key(entry, section, source) for entry in raw)


def _parse_key(entry: Any, section: str, source: str) -> ContractKey:
    if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
        raise ContractError(f"contract {source}: every {section}[] entry needs a 'key' string")
    name = entry["key"]

    value_type = entry.get("type")
    if value_type not in VALUE_TYPES:
        raise ContractError(f"{source}: {name}: type must be one of {sorted(VALUE_TYPES)}")
    required = entry.get("required")
    if not isinstance(required, bool):
        raise ContractError(f"{source}: {name}: required must be a boolean")

    enum = entry.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ContractError(f"{source}: {name}: enum must be a non-empty list")

    default = entry.get("default")
    key = ContractKey(
        key=name,
        type=value_type,
        required=required,
        default=default,
        enum=tuple(enum) if enum is not None else None,
    )
    if default is not None and key.enum is not None and default not in key.enum:
        raise ContractError(f"{source}: {name}: default {default!r} not in enum")
    if required and default is not None:
        raise ContractError(f"{source}: {name}: a required key cannot also have a default")
    return key
