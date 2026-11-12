"""开放字段须来自实际引用，凭据和未知插件默认不可写。"""

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.managed_fields import (
    AssemblyField,
    ManagedFields,
    apply_assembly_overrides,
)

pytestmark = pytest.mark.unit


def test_fields_restrict_target_and_url_and_hide_credentials():
    context = AssemblyContext.from_dict({"llm": {"main": "openai", "custom": "external"}})
    fields = ManagedFields(context, ["main", "custom"])
    assert "llm.main.params.llm_api_key" in fields.keys
    assert "llm.custom.target" not in fields.keys
    assert "prompts.extract.fact" not in fields.keys
    fields.validate({"llm.main.params.llm_base_url": "https://example.com"})
    with pytest.raises(ValidationError):
        fields.validate({"llm.main.params.enable_thinking": True})
    with pytest.raises(ValidationError):
        fields.validate({"prompts.extract.fact": "not editable"})
    fields.validate({"llm.main.target": "dashscope", "llm.main.params.enable_thinking": True})


def test_field_numeric_and_url_validation():
    context = AssemblyContext.from_dict({"llm": {"main": "openai"}})
    fields = ManagedFields(context, ["main"], url_allowed=lambda url: url == "https://ok.test")
    fields.validate({"llm.main.params.llm_base_url": "https://ok.test"})
    for values in (
        {"llm.main.params.llm_timeout": 0},
        {"llm.main.params.llm_temperature": float("nan")},
        {"llm.main.params.llm_max_tokens": True},
        {"llm.main.params.llm_max_retries": -1},
        {"llm.main.params.llm_base_url": "https://user:secret@ok.test"},
    ):
        with pytest.raises(ValidationError):
            fields.validate(values)


def test_shared_explicit_namespace_cannot_be_edited_as_independent_instances():
    context = AssemblyContext.from_dict({"llm": {
        "default": "openai",
        "other": {"target": "openai", "params": {"config_namespace": "llm"}},
    }})
    fields = ManagedFields(context, ["default", "other"])
    assert fields.readonly_reason("llm.default.params.llm_model")
    assert fields.readonly_reason("llm.other.params.llm_base_url")
    with pytest.raises(ValidationError):
        fields.validate({"llm.other.params.llm_model": "new"})


def test_replaced_llm_plugin_does_not_inherit_supported_field_capability():
    from jiuwen_memory.common.llm.base import LlmProducer

    context = AssemblyContext.from_dict({"llm": {"main": "openai"}})
    assert "llm.main.target" in ManagedFields(context, ["main"]).keys
    with LlmProducer.registration_scope():
        LlmProducer.register("openai")(lambda config: None)
        fields = ManagedFields(context, ["main"])
        assert "llm.main.target" not in fields.keys


def test_overlay_preserves_base_and_other_instance_params():
    base = AssemblyContext.from_dict(
        {
            "llm": {
                "main": {
                    "target": "openai",
                    "new_instance": True,
                    "params": {
                        "llm_model": "old",
                        "tokenizer": "custom",
                        "llm_temperature": 0.2,
                    },
                },
                "other": {"target": "echo"},
            },
            "prompts": {"extract": {"fact": "old", "other": "keep"}},
        }
    )
    result = apply_assembly_overrides(
        base,
        {"model": "new", "target": "dashscope"},
        {
            "model": AssemblyField("llm", "main", "llm_model"),
            "target": AssemblyField("llm", "main", "target"),
        },
    )
    main = result.lookup("llm", "main")
    assert main.target == "dashscope"
    assert main.new_instance is True
    assert main.params_explicit is True
    assert main.params == {"llm_model": "new", "tokenizer": "custom", "llm_temperature": 0.2}
    assert result.lookup("llm", "other").target == "echo"
    assert result.globals["prompts"]["extract"] == {"fact": "old", "other": "keep"}
    assert base.lookup("llm", "main").target == "openai"
    assert base.globals["prompts"]["extract"]["fact"] == "old"


def test_overlay_rejects_unknown_fields_and_instances():
    base = AssemblyContext.from_dict({"llm": {"main": "echo"}})
    with pytest.raises(ValidationError):
        apply_assembly_overrides(base, {"unknown": "value"}, {})
    with pytest.raises(ValidationError):
        apply_assembly_overrides(
            base, {"model": "new"}, {"model": AssemblyField("llm", "other", "llm_model")}
        )
