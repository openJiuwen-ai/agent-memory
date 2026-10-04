"""membership_impl 实现集：import 触发 MembershipProducer 自注册。"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.membership import MembershipProducer

import_optional(".kv_membership_resolver", __name__)

__all__ = ["MembershipProducer"]
