"""最小实现：:class:`~common.normalizer.base.Normalizer` 的文本直通规约器。

仅支持 TEXT 模态：从 ``RawPayload`` 取出 UTF-8 文本作为 content 投影
（无 data 时回退到 uri）。无任何外部模型/服务依赖。
"""

from __future__ import annotations

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.normalizer.base import Normalizer, NormalizerProducer
from jiuwen_memory.common.type_def import Modality, RawPayload


class PassthroughNormalizer(Normalizer):
    """文本直通规约：从 ``RawPayload`` 取出 UTF-8 文本作为 content 投影。"""

    def plugin_type(self) -> PluginType:
        """返回当前插件类型。

        Returns:
            返回 PluginType。
        """
        return PluginType.NORMALIZER

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def modalities(self) -> list[Modality]:
        """执行 `modalities` 操作。

        Returns:
            返回 list[Modality]。
        """
        return [Modality.TEXT]

    def normalize(self, payload: RawPayload) -> str:
        """规范化输入值。

        Args:
            payload: 参数 payload（RawPayload）。

        Returns:
            返回 str。

        Raises:
            ValidationError: 执行失败时抛出。
        """
        if payload.data:
            return payload.data.decode("utf-8")
        if payload.uri:
            return payload.uri
        raise ValidationError("RawPayload 既无 data 也无 uri，无法规约出文本投影")


# -- 注册到 NormalizerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@NormalizerProducer.register("passthrough")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return PassthroughNormalizer()
