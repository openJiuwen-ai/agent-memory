"""membership_impl 实现集：import 触发 MembershipProducer 自注册。"""

from importlib import import_module

from jiuwen_memory.control.membership import MembershipProducer

import_module(".kv_membership_resolver", __name__)

__all__ = ["MembershipProducer"]
