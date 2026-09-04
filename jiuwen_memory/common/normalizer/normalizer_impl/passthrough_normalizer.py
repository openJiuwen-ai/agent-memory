# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~common.normalizer.base.Normalizer` 的文本直通规约器。

支持已是文本表示的 TEXT/CODE 模态：从 ``RawPayload`` 取出 UTF-8 文本作为
content 投影；只有 TEXT 在无 data 时兼容回退到 uri。无任何外部模型/服务依赖。
"""

from __future__ import annotations

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.normalizer.base import (
    Normalizer,
    NormalizerProducer,
    ensure_normalizer_supports,
)
from jiuwen_memory.common.type_def import Modality, RawPayload


class PassthroughNormalizer(Normalizer):
    """文本直通规约：从 ``RawPayload`` 取出 UTF-8 文本作为 content 投影。"""

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.NORMALIZER

    @staticmethod
    def health() -> None:
        return None

    @staticmethod
    def modalities() -> list[Modality]:
        return [Modality.TEXT, Modality.CODE]

    def normalize(self, payload: RawPayload) -> str:
        ensure_normalizer_supports(self, payload.modality)
        if payload.data:
            try:
                return payload.data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError("passthrough normalizer requires UTF-8 text data") from exc
        if payload.uri and payload.modality == Modality.TEXT:
            return payload.uri
        if payload.uri:
            raise ValidationError("passthrough normalizer only supports URI fallback for TEXT")
        raise ValidationError("RawPayload 既无 data 也无 uri，无法规约出文本投影")


# -- 注册到 NormalizerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@NormalizerProducer.register("passthrough")
def _build(config):
    return PassthroughNormalizer()
