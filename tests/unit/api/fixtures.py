"""API 测试中的显式委托装配；双主体 Scope 只描述资源归属，不作为认证 actor。"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security._delegation_binding import _stores
from jiuwen_memory.common.security.types import DELEGATABLE_ACTIONS, Delegation
from jiuwen_memory.common.type_def import Scope
from tests.support.scoped_authenticator import ScopedAuthenticator


def security_for(api, principal: Scope):
    # 白盒夹具须向实际 PDP 使用的委托真源写入记录，不新增公共装配接口。
    # pylint: disable=protected-access
    if not (principal.user and principal.agent):
        return internal_context(ScopedAuthenticator(principal))
    actor = Scope(org=principal.org, agent=principal.agent, session=principal.session)
    delegation_id = uuid4().hex
    record = Delegation(
        delegation_id=delegation_id,
        delegator=Scope(org=principal.org, user=principal.user),
        delegate=actor,
        actions=DELEGATABLE_ACTIONS,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    for store in _stores(api._authorizer):
        store.add(record)
    return internal_context(ScopedAuthenticator(actor, delegation_id=delegation_id))
