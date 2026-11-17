"""不调用候选对象方法的安全边界类型验证。"""


def has_exact_type(value, expected: type | tuple[type, ...]) -> bool:
    """只接受声明的具体类型，拒绝 bool 冒充 int 或可改写行为的容器子类。

    普通 isinstance 检查接受子类，且可能读取候选对象伪装的 __class__。
    本检查只读取解释器提供的真实类型，不调用不可信对象上的转换或属性。
    """
    expected_types = expected if isinstance(expected, tuple) else (expected,)
    actual_type = type(value)
    return any(actual_type is candidate for candidate in expected_types)
