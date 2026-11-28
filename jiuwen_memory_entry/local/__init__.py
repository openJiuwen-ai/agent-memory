# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Local memory service (F17): data home, discovery, host hooks, install and CLI.

Only :mod:`jiuwen_memory_entry.local.discovery` is imported from short-lived host
hook processes, so it must stay standard-library only; the rest of the package may
use the kernel and entry-side dependencies.
"""
