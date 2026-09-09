"""membership_impl 实现集：import 触发 MembershipProducer 自注册。"""

import logging
from importlib import import_module

from jiuwen_memory.control.membership import MembershipProducer

try:
    import_module(".kv_membership_resolver", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["MembershipProducer"]
