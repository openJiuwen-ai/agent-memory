"""认证数据类型：认证模式与凭据材料（security.md §2.2）。

纯数据定义，不依赖本层其他文件——与 ``control/types.py`` 的地位一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class AuthMode(str, Enum):
    """认证模式（security.md §2.2）。

    刻意**不定义** ``OAUTH``：OAuth 2.1 是第二期（§2.4）。定义一个没有实现的
    枚举值，只会让 ``target: oauth`` 得到「未注册的实现」这种间接报错。
    第二期加它时是纯新增。
    """

    DEV = "dev"  # 无认证，恒返回 ROOT；只允许 localhost 绑定
    TRUSTED = "trusted"  # 信任上游网关已认证，只读网关注入的身份声明
    API_KEY = "api_key"  # 框架自校验 API Key


@dataclass(frozen=True)
class Credentials:
    """一次请求携带的原始凭据材料，由各 surface（HTTP / MCP / CLI）归一后传入。

    认证层不认识 HTTP：若直接把 ``http.client.HTTPMessage`` 传进
    :class:`~common.authentication.base.Authenticator`，实现里就会出现传输层耦合，
    MCP / CLI 无法复用。这里只保留认证需要的三样东西。

    ``headers`` 仍保留，因为 TRUSTED 模式的语义就是「读网关注入的 header」
    （§2.2.2），无法进一步抽象；但**只有 TRUSTED 实现读它**。键**必须已归一
    为小写**（HTTP header 名大小写不敏感，RFC 9110 §5.1），归一职责在
    ``bootstrap/core/auth_middleware.credentials_from_headers``。
    """

    api_key: str = ""  # Authorization: Bearer / X-Api-Key 提取后的裸 key
    headers: Mapping[str, str] = field(default_factory=dict)  # 小写键
    peer_address: str = ""  # 调用方地址，供审计与（未来）速率限制
