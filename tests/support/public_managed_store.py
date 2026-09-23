# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公共授权管理契约替身；跨测试共享同一类型，导入时不注册插件。"""

import threading
from copy import deepcopy
from dataclasses import replace

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.security.authorization.store import GrantStore


class PublicManagedStore(GrantStore):
    """只实现公开契约的第三方替身；没有内置 Store 的私有钩子。"""

    def __init__(self):
        self.records = {}
        self.lock = threading.RLock()

    def add(self, grant):
        with self.lock:
            old = self.records.get(grant.grant_id)
            if old is None or not old.revoked:
                self.records[grant.grant_id] = deepcopy(grant)

    def create_grant(self, grant):
        snapshot = deepcopy(grant)
        with self.lock:
            old = self.records.get(grant.grant_id)
            if old is not None:
                if old != snapshot:
                    raise BackendError("grant creation ID conflict")
                return
            self.records[grant.grant_id] = snapshot

    def get_grant(self, grant_id):
        with self.lock:
            return deepcopy(self.records.get(grant_id))

    def revoke_if_grantor(self, grant_id, expected_grantor):
        with self.lock:
            old = self.records.get(grant_id)
            if old is not None:
                if old.grantor != expected_grantor:
                    raise BackendError("grant changed during revocation")
                self.revoke(grant_id)

    def revoke(self, grant_id):
        with self.lock:
            old = self.records.get(grant_id)
            if old is not None:
                self.records[grant_id] = replace(old, revoked=True)

    def find_active(self, *, grantee, grantor_org, action, now):
        active = []
        with self.lock:
            records = deepcopy(tuple(self.records.values()))
        for grant in records:
            if grant.grantor.org != grantor_org or grant.grantee.org != grantee.org:
                continue
            if action in grant.actions and grant.is_active(now=now):
                active.append(deepcopy(grant))
        return active

    def health(self):
        return None
