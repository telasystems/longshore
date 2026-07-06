"""CLI wiring: argument handling and the render verb end-to-end over moto SSM."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from longshore.cli import CliError, _parse_command, main

from .conftest import PREFIX, REGION, SERVICE


@pytest.fixture()
def populated_ssm(minimal_params):
    with mock_aws():
        client = boto3.client("ssm", region_name=REGION)
        for key, value in minimal_params.items():
            client.put_parameter(Name=f"{PREFIX}/{key}", Type="String", Value=value)
        client.put_parameter(
            Name=f"{PREFIX}/containers/main/env/APP_ENV", Type="String", Value="staging"
        )
        yield client


def test_render_end_to_end(populated_ssm, capsys):
    code = main(["render", "--service", SERVICE, "--region", REGION, "--image", "repo:v1"])
    assert code == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["family"] == SERVICE
    container = rendered["containerDefinitions"][0]
    assert container["image"] == "repo:v1"
    assert [s["name"] for s in container["secrets"]] == ["APP_ENV"]
    assert container["logConfiguration"]["options"]["awslogs-region"] == REGION


def test_render_image_placeholder(populated_ssm, capsys):
    assert main(["render", "--service", SERVICE, "--region", REGION]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["containerDefinitions"][0]["image"] == "<image>"


def test_render_to_output_file(populated_ssm, tmp_path, capsys):
    out_file = tmp_path / "task-def.json"
    code = main(
        [
            "render",
            "--service",
            SERVICE,
            "--region",
            REGION,
            "--image",
            "repo:v1",
            "--output",
            str(out_file),
        ]
    )
    assert code == 0
    assert json.loads(out_file.read_text())["family"] == SERVICE


def test_unknown_service_is_a_clean_error(populated_ssm, capsys):
    code = main(["render", "--service", "no-such-service", "--region", REGION])
    assert code == 1
    err = capsys.readouterr().err
    assert "no SSM parameters found" in err


def test_validate_compliant_service(populated_ssm, capsys):
    code = main(["validate", "--service", SERVICE, "--region", REGION])
    out = capsys.readouterr().out
    assert code == 0, out
    assert f"{SERVICE} is compliant" in out
    assert "contract v1" in out
    assert "container main: 1 env param(s)" in out
    # Informational producer keys are surfaced but not violations.
    assert "note: ecr/url is not part of the deploy contract (ignored)" in out


def test_validate_reports_all_violations(populated_ssm, capsys):
    populated_ssm.delete_parameter(Name=f"{PREFIX}/ecs/execution_role_arn")
    populated_ssm.put_parameter(
        Name=f"{PREFIX}/ecs/cpu", Type="String", Value="not-a-number", Overwrite=True
    )
    code = main(["validate", "--service", SERVICE, "--region", REGION])
    out = capsys.readouterr().out
    assert code == 1
    assert "violation: missing required parameter: ecs/execution_role_arn" in out
    assert "violation: ecs/cpu: expected an integer" in out
    assert "NOT compliant (2 violation(s))" in out


def test_validate_catches_structural_problems(populated_ssm, capsys):
    # Key-level checks pass but the sidecar definition is broken.
    populated_ssm.put_parameter(
        Name=f"{PREFIX}/ecs/sidecars", Type="String", Value='[{"image": "x"}]'
    )
    code = main(["validate", "--service", SERVICE, "--region", REGION])
    out = capsys.readouterr().out
    assert code == 1
    assert "sidecar" in out


def test_verb_is_required(capsys):
    with pytest.raises(SystemExit):
        main([])


class TestParseCommand:
    def test_json_list(self):
        assert _parse_command('["php", "artisan", "migrate", "--force"]') == [
            "php",
            "artisan",
            "migrate",
            "--force",
        ]

    def test_shell_string(self):
        assert _parse_command("php artisan migrate --force") == [
            "php",
            "artisan",
            "migrate",
            "--force",
        ]

    def test_quoted_shell_string(self):
        assert _parse_command("sh -c 'echo hello world'") == ["sh", "-c", "echo hello world"]

    def test_bad_json_list(self):
        with pytest.raises(CliError):
            _parse_command("[1, 2, 3]")
