"""SSM-contract-driven ECS deployment tool.

Reads a service's contract from SSM Parameter Store (/services/{name}/*),
renders an ECS task definition, registers it, repoints the service, and
observes the rollout. Terraform owns the service; this tool owns only the
task definition. See docs/deployment-tooling.md in the infrastructure repo.
"""

__version__ = "0.1.0"
