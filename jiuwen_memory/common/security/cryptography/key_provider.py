"""密钥提供与轮换契约（F05 §Cryptography §KeyProvider）。

密码学能力**只能通过本接口获取密钥**，不得自己读环境变量或配置文件里的根密钥
——否则「密钥从哪来」这条最敏感的路径会散落在每个 provider 里，KMS/Vault/HSM
也就无从接入。

用途隔离（F05 §密钥隔离）由 :meth:`KeyProvider.wrap` 的 ``purpose`` 参数承担：
加密、审计完整性、token 签名各自派生独立子密钥。API Key 与 Encryption Root Key
永远不是同一密钥体系——前者归
:mod:`common.security.authentication.key_store`，两边不共享任何材料。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from jiuwen_memory.common.factory.factory import Factory


class KeyProviderProducer(Factory):
    """KeyProvider 的注册式工厂（与契约同处接口层）。

    各实现在 ``cryptography_impl`` 下以 ``@KeyProviderProducer.register("<后端>")``
    自注册，由 :func:`common.security.bootstrap.register_security` 统一触发。
    """

    TOP_NAME = "key_provider"


@dataclass(frozen=True)
class KeyRef:
    """一把密钥的标识：id + epoch（F05 §信封格式）。

    ``key_id`` 标识密钥体系中的哪一把，``epoch`` 标识它的第几代。两者都进信封头
    与 AAD：只有 id 没有 epoch 时，轮换后的密文可以被替换成同一把 key 的旧代密文
    而不被察觉。

    ``key_id`` 必须是**不可逆标识**（如密钥材料的 KDF 派生指纹），不得包含密钥
    材料本身——它会明文写进信封，随数据一起落盘。
    """

    key_id: str
    epoch: int = 1


@dataclass(frozen=True)
class WrappedKey:
    """被包裹的数据密钥及其解开所需的全部非敏感元数据。"""

    ciphertext: bytes
    nonce: bytes
    ref: KeyRef


class KeyProvider(ABC):
    """密钥的提供方：包裹/解开数据密钥，并声明当前活动密钥。"""

    @abstractmethod
    def active_key(self) -> KeyRef:
        """当前用于**加密**的密钥标识。解密按密文自带的 ref 走，不用这个。"""

    @abstractmethod
    def rotate(self) -> KeyRef:
        """轮换到新一代活动密钥，返回新的 :class:`KeyRef`（F05 §KeyProvider）。

        轮换后 :meth:`active_key` 与新 :meth:`wrap` 都落到新 epoch；旧 epoch 密文仍按
        自带 ref 解开，前提是实现保留了历史 epoch 的验证材料。能否安全轮换是
        KeyProvider 的契约能力，而不只是 ``active_key`` 之外的一个可选项--只有 epoch
        字段、没有轮换入口的实现不满足 F05。实现必须真正推进 epoch（或更换 key_id），
        不得永远抛错冒充 fail-closed。
        """

    @abstractmethod
    def wrap(self, data_key: bytes, *, purpose: str, org: str) -> WrappedKey:
        """用当前活动密钥包裹一把数据密钥。

        ``purpose`` 与 ``org`` 参与密钥派生与包裹层 AAD：前者实现用途隔离（F05
        §密钥隔离），后者实现租户隔离——两者不同的调用绝不能解开彼此的数据密钥。
        """

    @abstractmethod
    def unwrap(self, wrapped: WrappedKey, *, purpose: str, org: str) -> bytes:
        """解开数据密钥；失败抛
        :class:`~common.security.cryptography.base.KeyMismatchError`。

        必须按 ``wrapped.ref`` 选取密钥材料，而不是无条件用活动密钥——轮换后旧
        数据仍要可读，前提是实现保留了对应 epoch 的验证材料。找不到对应材料时
        **拒绝**，不得回退到活动密钥试解（那会让 epoch 绑定形同虚设）。
        """

    @abstractmethod
    def health(self) -> None:
        """存活探测：密钥可用时返回 ``None``，否则抛出异常。

        不得在异常消息里泄露密钥材料、密钥文件内容或 KMS 凭据（F05 §装配不变量 8）。
        """
