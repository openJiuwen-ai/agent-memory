"""Continuity implementations; registration is triggered by control bootstrap."""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.continuity import ContinuityProducer

import_optional(".service", __name__)

__all__ = ["ContinuityProducer"]
