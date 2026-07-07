"""Seed a moto server with the producer-side state the smoke test needs.

Plays the role of the producer's IaC: writes the /services/smoke SSM
prefix and bootstraps the ECS cluster/service that `longshore deploy`
(run via the composite action) will repoint.
"""

import os

import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")
ACCOUNT = "123456789012"
SERVICE = "smoke"
PREFIX = f"/services/{SERVICE}"

ssm = boto3.client("ssm", region_name=REGION)
ecs = boto3.client("ecs", region_name=REGION)

cluster_arn = ecs.create_cluster(clusterName="smoke-cluster")["cluster"]["clusterArn"]

config = {
    "ecs/cluster_arn": cluster_arn,
    "ecs/execution_role_arn": f"arn:aws:iam::{ACCOUNT}:role/{SERVICE}-exec",
    "ecs/task_role_arn": f"arn:aws:iam::{ACCOUNT}:role/{SERVICE}-task",
    "ecs/log_group_name": f"/ecs/{SERVICE}",
    "ecs/container_port": "8080",
}
for key, value in config.items():
    ssm.put_parameter(Name=f"{PREFIX}/{key}", Value=value, Type="String")

# One SecureString env param to exercise per-container env discovery.
ssm.put_parameter(Name=f"{PREFIX}/containers/main/env/APP_ENV", Value="smoke", Type="SecureString")

# The producer bootstraps the service with a placeholder task definition;
# longshore only ever registers new revisions and repoints.
bootstrap = ecs.register_task_definition(
    family=SERVICE,
    requiresCompatibilities=["FARGATE"],
    networkMode="awsvpc",
    cpu="256",
    memory="512",
    containerDefinitions=[{"name": "main", "image": "bootstrap", "essential": True}],
)["taskDefinition"]["taskDefinitionArn"]
ecs.create_service(
    cluster=cluster_arn,
    serviceName=SERVICE,
    taskDefinition=bootstrap,
    desiredCount=1,
    launchType="FARGATE",
)
print(f"seeded {PREFIX} and ECS service {SERVICE!r} in {cluster_arn}")
