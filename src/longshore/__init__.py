"""SSM-contract-driven ECS deployment tool.

Reads a service's contract from SSM Parameter Store (/services/{name}/*),
renders an ECS task definition, registers it, repoints the service, and
observes the rollout. Terraform owns the service; this tool owns only the
task definition. See docs/deployment-tooling.md in the infrastructure repo.
"""

from importlib.metadata import version

# Single source of truth is pyproject.toml; resolved from installed metadata.
__version__ = version("longshore")
