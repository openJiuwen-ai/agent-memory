# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AuditEvent — 审计事件（横切结构，架构 §12）。

各层（接入/构建/检索/控制）的关键动作都产生同一结构的审计事件：
谁（actor scope）在何时对哪个对象做了什么。控制层治理接口按此查询/
回溯，存储层审计后端负责持久化。

"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .scope import Scope


@dataclass
class AuditEvent:
    id: str = ""  # 事件 id
    actor: Scope = field(default_factory=Scope)  # 操作者 scope
    action: str = ""  # 动作：write / update / delete / recall / evolve / grant ...
    target_id: str = ""  # 作用对象（记忆单元 id / 配置项 / scope 等）
    layer: str = ""  # 产生事件的层：ingest / construction / retrieval / control
    decision: str = ""  # allow / deny
    occurred_at: datetime | None = None  # 发生时间
    detail: dict[str, str] = field(
        default_factory=dict
    )  # 附加明细（字符串化扩展字段；不放敏感 scope）
    target: Scope = field(default_factory=Scope)  # 操作目标 scope；无具体目标时为空
    # 常见约定：permission_check、permission_reason、job_id、
    # before_unit_id / after_unit_id、before_unit_ids / after_unit_ids
    # 安全层（src/security）另加四个：acting_user、role、key_fp、auth_mode。
    # security.md §7.2 要求审计记录这四样，但它们是**认证元数据**，与本结构
    # 承载的「谁对什么做了什么」不同层；塞 detail 而非提升为一等字段，是因为
    # 改本结构要同时动 common / control / 两个 AuditLogger 实现 +
    # handler._event_view。若这些键稳定使用，第二期应提升为一等字段。
