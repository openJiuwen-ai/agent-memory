"""共享类型验证不扩大 JSON 及宿主参数的安全边界。"""

import pytest

from jiuwen_memory_adapter.agent_security._validation import has_exact_type
from jiuwen_memory_adapter.agent_security.llm import chat_endpoint
from jiuwen_memory_adapter.agent_security.runtime import SecurityDenied, encode_payload

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["text", 1, 1.5, True, {}, [], ()])
def test_exact_builtin_values_keep_their_identity(value):
    assert has_exact_type(value, type(value))


def test_bool_and_behavior_overriding_subclasses_are_not_plain_values():
    class RewrittenDict(dict):
        def items(self):
            raise AssertionError("不应读取未验证容器")

    assert not has_exact_type(True, int)
    assert not has_exact_type(RewrittenDict(), dict)
    with pytest.raises(SecurityDenied):
        encode_payload(RewrittenDict())


def test_type_checks_do_not_read_spoofed_class_or_compare_metaclasses():
    class SpoofedClass:
        def __getattribute__(self, name):
            if name == "__class__":
                raise AssertionError("不应读取候选对象的 __class__")
            return object.__getattribute__(self, name)

    class SpoofedType(type):
        def __eq__(cls, other):
            raise AssertionError("类型验证必须按身份比较")

    class SpoofedValue(metaclass=SpoofedType):
        """伪装类型不能改变对内置值的判定。"""

    assert not has_exact_type(SpoofedClass(), dict)
    assert not has_exact_type(SpoofedValue(), (str, int, float, bool))


@pytest.mark.parametrize(
    "base",
    [
        "https://api.example/v1?route=other",
        "https://api.example/v1#fragment",
        "https://user:secret@api.example/v1",
        "\nhttps://api.example/v1",
        "https://api.example/\tv1",
    ],
)
def test_url_path_assembly_keeps_forbidden_metadata_visible_to_the_gate(base):
    with pytest.raises(SecurityDenied):
        chat_endpoint(base)
