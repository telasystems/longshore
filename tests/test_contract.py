"""Contract well-formedness and contract pinning.

The pinning tests are the mechanism that stops the bundled contract and the
renderer from drifting apart: every task-definition key in the contract must
be implemented (and every implemented key declared), every default must
materialize in rendered output, every required key must hard-fail when
absent, and every enum must reject values outside its set.
"""

from __future__ import annotations

import pytest

from longshore.contract import ContractError, parse_contract
from longshore.render import (
    RenderError,
    compliance_report,
    contract_keys_handled,
    render_task_definition,
    typed_config,
)

from .conftest import IMAGE, REGION, SERVICE


def render_minimal(params, contract):
    return render_task_definition(SERVICE, IMAGE, params, {}, REGION, contract)


class TestContract:
    def test_loads_and_is_well_formed(self, contract):
        assert contract.version == 1
        assert contract.prefix == "/services/{name}"
        assert len(contract.task_definition_keys) > 15
        assert {k.key for k in contract.deploy_keys} == {
            "ecs/cluster_arn",
            "ecs/subnets",
            "ecs/security_group_ids",
        }
        assert "{container}" in contract.env["path"]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"version": "2"},  # unsupported version
            {"prefix": "/services"},  # prefix without {name}
            {"task_definition": "[]"},  # empty section
            {"deploy": "[]"},
            {"env_path": "nope"},  # env path without {container}
            # unknown value type
            {"task_definition": "[{key: a, type: nope, required: false}]"},
            # same key in both sections
            {"deploy": "[{key: a, type: string, required: true}]"},
        ],
    )
    def test_malformed_contracts_are_rejected(self, overrides):
        doc = {
            "version": "1",
            "prefix": "/services/{name}",
            "deploy": "[{key: d, type: string, required: true}]",
            "task_definition": "[{key: a, type: string, required: false}]",
            "env_path": "containers/{container}/env/{VAR}",
        }
        doc.update(overrides)
        text = (
            f"version: {doc['version']}\n"
            f"prefix: {doc['prefix']}\n"
            f"deploy: {doc['deploy']}\n"
            f"task_definition: {doc['task_definition']}\n"
            f"env:\n  path: {doc['env_path']}\n"
        )
        with pytest.raises(ContractError):
            parse_contract(text, "test")


class TestPinning:
    """The bundled contract and render.py cannot drift silently."""

    def test_renderer_implements_exactly_the_contract(self, contract):
        declared = {k.key for k in contract.task_definition_keys}
        implemented = contract_keys_handled()
        assert declared == implemented, (
            f"contract but not renderer: {sorted(declared - implemented)}; "
            f"renderer but not contract: {sorted(implemented - declared)}"
        )

    def test_every_required_key_fails_when_absent(self, contract, minimal_params):
        required = [k.key for k in contract.task_definition_keys if k.required]
        assert required, "the contract should declare required task-definition keys"
        for key in required:
            params = dict(minimal_params)
            del params[key]
            with pytest.raises(RenderError, match=key):
                render_minimal(params, contract)

    def test_every_default_materializes(self, contract, minimal_params):
        # Render with defaultable keys removed; assert each contract default
        # lands at its mapped task-def location.
        extractors = {
            "ecs/cpu": lambda td: td["cpu"],
            "ecs/memory": lambda td: td["memory"],
            "ecs/cpu_architecture": lambda td: td["runtimePlatform"]["cpuArchitecture"],
            "ecs/container_name": lambda td: td["containerDefinitions"][0]["name"],
            "ecs/stop_timeout": lambda td: td["containerDefinitions"][0]["stopTimeout"],
            "ecs/init_process": lambda td: td["containerDefinitions"][0]["linuxParameters"][
                "initProcessEnabled"
            ],
            "ecs/service_connect_app_protocol": lambda td: td["containerDefinitions"][0][
                "portMappings"
            ][0]["appProtocol"],
        }
        defaulted = {
            k.key: k.default for k in contract.task_definition_keys if k.default is not None
        }
        assert set(extractors) == set(defaulted), (
            "a contract key gained/lost a default — update this extractor map"
        )

        params = {k: v for k, v in minimal_params.items() if k not in defaulted}
        # service_connect_app_protocol's default only appears with Service
        # Connect on, so switch it on without setting the protocol.
        params["ecs/service_connect_namespace_arn"] = "arn:aws:servicediscovery:::namespace/n"
        rendered = render_minimal(params, contract)
        for key, default in defaulted.items():
            observed = extractors[key](rendered)
            expected = str(default) if key in ("ecs/cpu", "ecs/memory") else default
            assert observed == expected, f"{key}: expected default {expected!r}, got {observed!r}"

    def test_every_enum_rejects_bad_values(self, contract, minimal_params):
        enum_keys = [k for k in contract.task_definition_keys if k.enum is not None]
        assert enum_keys, "the contract should declare enum keys"
        for key in enum_keys:
            params = dict(minimal_params)
            params[key.key] = "NOT_A_REAL_VALUE"
            with pytest.raises(ContractError, match=key.key):
                typed_config(params, contract)

    def test_every_typed_key_rejects_garbage(self, contract, minimal_params):
        for key in contract.task_definition_keys:
            if key.type == "string":
                continue
            params = dict(minimal_params)
            params[key.key] = "not-parseable-as-int-bool-or-json"
            with pytest.raises(ContractError, match=key.key):
                typed_config(params, contract)

    def test_fixed_fields_section_matches_renderer(self, contract, minimal_params):
        rendered = render_minimal(minimal_params, contract)
        fixed = contract.fixed_fields
        assert rendered["requiresCompatibilities"] == fixed["requiresCompatibilities"]
        assert rendered["networkMode"] == fixed["networkMode"]
        assert (
            rendered["runtimePlatform"]["operatingSystemFamily"]
            == fixed["runtimePlatform.operatingSystemFamily"]
        )
        assert rendered["containerDefinitions"][0]["essential"] is True
        assert rendered["containerDefinitions"][0]["image"] == IMAGE
        assert rendered["family"] == SERVICE


class TestComplianceReport:
    def test_compliant_params_have_no_issues(self, contract, full_params):
        issues, ignored = compliance_report(full_params, contract)
        assert issues == []
        # Informational keys the producer writes but the deployer ignores.
        assert ignored == ["ecr/url", "kms/key_arn"]

    def test_missing_required_and_bad_values_reported_together(self, contract, minimal_params):
        del minimal_params["ecs/execution_role_arn"]
        minimal_params["ecs/cpu"] = "not-a-number"
        minimal_params["ecs/cpu_architecture"] = "SPARC"
        issues, _ = compliance_report(minimal_params, contract)
        assert len(issues) == 3
        joined = "\n".join(issues)
        assert "ecs/execution_role_arn" in joined
        assert "ecs/cpu" in joined
        assert "SPARC" in joined

    def test_deploy_section_is_checked(self, contract, minimal_params):
        del minimal_params["ecs/cluster_arn"]
        issues, _ = compliance_report(minimal_params, contract)
        assert any("ecs/cluster_arn" in issue for issue in issues)
