"""ConfigSource 实现包：触发 yaml_defaults / dict / overlay 自注册。"""

from __future__ import annotations

_REGISTERED = False


def register_config_sources() -> None:
    """幂等：import 实现模块以触发 ``@ConfigSourceProducer.register``。"""
    global _REGISTERED
    if _REGISTERED:
        return
    from . import dict_config_source as _dict  # noqa: F401
    from . import overlay_config_source as _overlay  # noqa: F401
    from . import yaml_defaults_config_source as _yaml_defaults  # noqa: F401

    _REGISTERED = True
