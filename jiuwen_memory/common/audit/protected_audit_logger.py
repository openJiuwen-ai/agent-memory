# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ProtectedAuditLogger - 把普通 AuditLogger 升级为链式完整性审计（F05 §Audit Integrity）。

启用审计完整性期时，PEP（LocalMemoryAPI）与 surface 记录入口事件都不再直连
:class:`~jiuwen_memory.common.audit.base.AuditLogger`，而是经本 wrapper：

- ``record(event)`` -> :meth:`AuditIntegrityProvider.record_chained`：规范化、计算 proof、
  原子追加进链。关键事件写入失败时 provider 抛
  :class:`~jiuwen_memory.common.security.audit_integrity.base.AuditIntegrityError`，由调用方
  按事件等级 fail-closed--wrapper 不吞错、不降级为无完整性审计。
- ``query(filters, limit)`` -> 底层 AuditLogger.query：链式后端的 query 从链读取
  （脱去 proof），故查询经此透传即可返回事件。

wrapper 不持有需自管 ``close`` 的资源：构造时通过 ``provider.chain_store()`` 对象 identity
校验其与 AuditLogger 是同一实例，而不是只靠具名配置或注释约定；资源仍由审计日志的
生命周期所有者统一关闭。

**统一完整性错误语义（R1）**：受保护写路径上的后端（如 SQLite 触发器 / 磁盘故障）、
密钥或序列化故障一律归一为 ``AuditIntegrityError`` 族再上抛——认证入口等调用方按
``integrity_protected`` 标记 fail-closed，不靠「正好抛了某个异常类型」区分受保护与
普通审计。任何一次受保护追加失败都置 degraded 闩（计划 §9.3：标记 Runtime 不健康并
隔离），闩经 ``integrity_degraded`` 暴露，mutation 入口在业务副作用前检查它。

**detail 统一隐私过滤（计划 §10）**：写入前对 ``AuditEvent.detail`` 做类型/长度限制与
敏感键脱敏，再进 proof 计算——过滤是规范化的一部分，验证按存储后的事件重算即一致。
"""

from __future__ import annotations

from dataclasses import replace

from jiuwen_memory.common.audit.base import AuditLogger
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditIntegrityError,
    AuditIntegrityProvider,
)
from jiuwen_memory.common.type_def import AuditEvent

# detail 的有界边界（计划 §10：超大 detail 不得成为签名/存储 DoS）。
_MAX_DETAIL_KEYS = 64
_MAX_DETAIL_VALUE_CHARS = 1024
_MAX_DETAIL_KEY_CHARS = 256
_TRUNCATED_SUFFIX = "...[truncated]"
_REDACTED = "[redacted]"

# 敏感键词干（计划 §10 禁入清单的键形态）：api key / token / 口令 / 密钥材料等。
# 匹配键名（小写包含），不匹配值——脱敏按键决定，值不参与判断。
_SENSITIVE_KEY_STEMS = (
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "credential",
    "private_key",
    "key_material",
    "key_hex",
    "key_b64",
    # 第三轮 T3 补充的禁入内容键名（计划 §10 列明的禁入字段）：
    "content",
    "headers",
    "root_key",
    "apikey",
    # 第四轮 Q3：计划 §10 明确禁止 audit key 材料。
    "audit_key",
    # 第五轮 U2：计划 §10 列明的 KDF 中间材料（kdf_output / kdf_material 等）。
    "kdf",
)

# 已知非秘密标识字段（U2 复核）：计划 §10 与服务端审计调用点确认的可追溯字段名。
# 这些字段携带的是标识而非秘密材料本身，即使键名包含敏感词干也不脱敏。
# 必须是确切的字段名，不得泛化为后缀/前缀/通配——未在此列表中的扩展字段
# 即使形似标识后缀也按敏感词干正常判断。
_KNOWN_NON_SENSITIVE_FIELDS = frozenset(
    {
        "credential_id",  # 凭据标识（非凭据本身）
        "key_fp",  # 密钥指纹（非密钥材料）
    }
)

# detail 的系统保留键（T5）：服务端权威写入，受保护写入过滤不得丢弃。
_SYSTEM_DETAIL_KEYS = frozenset({"decision", "request_id"})


def _sanitize_detail(event: AuditEvent) -> AuditEvent:
    """写入前的 detail 类型/长度限制与敏感键脱敏（计划 §10；T3/T4/T5/Q3）。

    - 键值统一 ``str`` 规范化（与 canonical 序列化的 str 化语义一致，存储不再出现
      混合类型）；
    - 敏感词干匹配在键名截断**之前**执行——截断可能从原键名尾部丢失敏感标记（Q3）；
    - 键名超长截断、单值超长截断、键数超限丢弃尾部，保证 detail 有界；
    - 键名命中敏感词干的值整值替换为固定标记，不进链、不进存储；
    - 已知非秘密标识字段（credential_id / key_fp）命中敏感词干仍保留原值（U2）；
    - 系统键（decision / request_id）不计入 64 键预算，始终保留（T5）。
    """
    if not event.detail:
        return event
    safe: dict[str, str] = {}
    # 有界迭代（T4）：不物化全部 items，最多取 _MAX_DETAIL_KEYS 个非系统键。
    consumed = 0
    for key, value in event.detail.items():
        name = key if isinstance(key, str) else str(key)
        # 系统键不占预算，不截断（T5）
        if key in _SYSTEM_DETAIL_KEYS:
            safe[name] = _sanitize_value(value)
            continue
        if consumed >= _MAX_DETAIL_KEYS:
            break
        consumed += 1
        # Q3：敏感词干匹配必须在键名截断之前——完整键名才能可靠检测敏感标记。
        lowered = name.lower()
        # U2 复核：已知非秘密标识字段（credential_id / key_fp）不脱敏。
        # 必须是确切的字段名——后缀泛化会将 raw_password_id 等未批准字段也放行。
        if name in _KNOWN_NON_SENSITIVE_FIELDS:
            safe[_sanitize_key_name(name)] = _sanitize_value(value)
            continue
        if any(stem in lowered for stem in _SENSITIVE_KEY_STEMS):
            safe[_sanitize_key_name(name)] = _REDACTED
            continue
        safe[_sanitize_key_name(name)] = _sanitize_value(value)
    # T5 兜底：系统键可能因键名变换（key → name 截断）尚未写入 safe；再次确保。
    for sys_key in _SYSTEM_DETAIL_KEYS:
        if sys_key in event.detail and sys_key not in safe:
            safe[sys_key] = _sanitize_value(event.detail[sys_key])
    return replace(event, detail=safe)


def _sanitize_key_name(name: str) -> str:
    """键名超长截断（T4）；调用方应先在完整键名上完成敏感词干匹配（Q3）。"""
    if len(name) > _MAX_DETAIL_KEY_CHARS:
        return name[:_MAX_DETAIL_KEY_CHARS] + _TRUNCATED_SUFFIX
    return name


def _sanitize_value(value: object) -> str:
    """单值 str 化 + 超长截断。"""
    text = value if isinstance(value, str) else str(value)
    if len(text) > _MAX_DETAIL_VALUE_CHARS:
        text = text[:_MAX_DETAIL_VALUE_CHARS] + _TRUNCATED_SUFFIX
    return text


class ProtectedAuditLogger(AuditLogger):
    """把 record 转为链式完整性写入、query 透传底层后端的 AuditLogger。"""

    #: 受保护审计标记：认证入口据此对**任何**写入失败 fail-closed（R1），普通
    #: AuditLogger 实现没有该标记，保持既有尽力而为语义。
    integrity_protected = True

    def __init__(self, provider: AuditIntegrityProvider, audit_logger: AuditLogger) -> None:
        if provider.chain_store() is not audit_logger:
            raise ValidationError(
                "audit integrity provider and AuditLogger must use the same store instance"
            )
        self._provider = provider
        self._audit = audit_logger
        # 受保护追加失败闩（计划 §9.3）：一旦置位，Runtime 不健康、mutation 隔离，
        # 直到进程重启或运维修复后重建 Runtime；不在本类内自动清除——「已丢失证据」
        # 的状态不能靠下一次写入成功抹掉。
        self._degraded = False

    @property
    def integrity_degraded(self) -> bool:
        """受保护链是否已发生追加失败（Runtime 不健康，mutation 应被隔离）。"""
        return self._degraded

    def record(self, event: AuditEvent) -> None:
        """经 provider 记链；写入失败统一抛 :class:`AuditIntegrityError`（fail-closed）。

        后端/密钥/序列化故障不属于完整性族的原生异常也在此归一（R1）：受保护写路径
        对调用方只有一种失败语义。任何失败都置 degraded 闩。
        """
        try:
            self._provider.record_chained(_sanitize_detail(event))
        except AuditIntegrityError:
            self._degraded = True
            raise
        except Exception as exc:
            self._degraded = True
            raise AuditIntegrityError(
                f"protected audit append failed: {type(exc).__name__}"
            ) from exc

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        """查询透传底层后端（链式模式下读 chain 记录，脱 proof）。"""
        return self._audit.query(filters, limit)
