"""原会话审批收件箱；决定入口要求宿主认证的 Actor，不解析聊天文字。"""

import json
import threading

from ._validation import has_exact_type
from .runtime import ApprovalChoice, SecurityDenied, encode_payload


class ApprovalInbox:
    def __init__(self, broker, *, notify=None, capacity=1024):
        if not has_exact_type(capacity, int) or capacity < 1:
            raise ValueError("审批收件箱容量必须为正整数")
        self.broker, self.notify, self.capacity = broker, notify, capacity
        self._requests, self._lock, self._closed = {}, threading.RLock(), False

    def present(self, public, *, actor):
        public = json.loads(encode_payload(public))
        with self._lock:
            if self._closed or len(self._requests) >= self.capacity:
                raise SecurityDenied("审批界面不可用或容量已满")
            approval_id = public["approval_id"]
            # Broker 校验请求存在；版本、候选只来自插件，不接受 UI 自行登记请求。
            original = self.broker.public_request(approval_id, actor=actor)
            if any(public.get(key) != value for key, value in original.items()):
                raise SecurityDenied("审批展示与可信候选不一致")
            self._requests[approval_id] = (actor, public)
        try:
            if self.notify is not None:
                self.notify(actor, {"type": "jiuwen.security.changed"})
        except Exception:
            self.broker.cancel(approval_id)
            raise SecurityDenied("审批消息投递失败") from None

    def pending(self, actor):
        from .runtime import ApprovalState

        with self._lock:
            if self._closed:
                raise SecurityDenied("审批界面已关闭")
            requests = []
            for key, (owner, public) in tuple(self._requests.items()):
                if self.broker.state(key) != ApprovalState.WAITING:
                    del self._requests[key]
                elif owner == actor:
                    requests.append(json.loads(encode_payload(public)))
            return tuple(requests)

    def decide(self, *, actor, approval_id, version, choice):
        with self._lock:
            item = self._requests.get(approval_id)
            if self._closed or item is None or item[0] != actor:
                raise SecurityDenied("此主体不能审批该会话的操作")
            self.broker.submit(approval_id, version, ApprovalChoice(choice), reviewer=actor)

    def close(self):
        with self._lock:
            self._closed = True
            for approval_id in self._requests:
                self.broker.cancel(approval_id)
            self._requests.clear()


def approval_reply(public_request, choice):
    """现有 UI 的选择处理器调用；身份仍必须由宿主当前认证上下文提供。"""
    choice = ApprovalChoice(choice)
    if choice not in {option["value"] for option in public_request["options"]}:
        raise SecurityDenied("选项不允许")
    return {
        "approval_id": public_request["approval_id"],
        "version": public_request["version"],
        "choice": str(choice),
    }
