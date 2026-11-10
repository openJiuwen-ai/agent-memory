# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Normalizer — 模态规约能力：原模态数据 -> 可治理文本/结构投影。

**共用说明**：接入层写入时对每种模态规约出 ``content`` 投影（图像 →
caption/OCR/视觉描述，音频 → ASR 转录，视频 → 关键帧描述 + 转录，
文档/代码 → 解析）；构建层/自演进的**重建路径**必须重跑同一规约器
来重建投影（架构 §10.1「重跑规约器即可重建投影」）——两处不是同一
实现，投影就不可复现、派生不可重建。底层 ASR/OCR/caption 模型可插拔，
端侧可降级或延迟到云侧。
"""

from __future__ import annotations

from abc import abstractmethod

from ..base import Plugin
from ..errors import UnsupportedCapabilityError, ValidationError
from ..factory.factory import Factory
from ..type_def import Modality, RawPayload


class NormalizerProducer(Factory):
    """Normalizer 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``normalizer_impl`` 下以
    ``@NormalizerProducer.register("<名>")`` 自注册。注册发生在 import 实现模块时，
    由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "normalizer"


class Normalizer(Plugin):
    @abstractmethod
    def modalities(self) -> list[Modality]:
        """返回本规约器支持的模态。"""

    @abstractmethod
    def normalize(self, payload: RawPayload) -> str:
        """将原模态负载规约为可治理的文本/结构投影（content）。"""

    def structured_modalities(self) -> frozenset[Modality]:
        """返回规约结果为**结构化中间产物**的模态子集（缺省空集）。

        这些模态的 content 是给下游程序整体解析的包络（如视频规约的 JSON，由
        VideoMemoryExtractor ``json.loads`` 后拆成 CLM/ELM），接入层不得按长度预切分；
        未声明的模态一律视为整块纯文本投影，照常预切分。声明与产出同处一个实现——
        谁生产该格式，谁负责声明；组合型实现（如 RoutingNormalizer）按路由汇总 delegate
        的声明。
        """
        return frozenset()


def ensure_structured_modalities_declared(normalizer: Normalizer) -> frozenset[Modality]:
    """装配期门禁：``structured_modalities()`` 必须是 ``modalities()`` 的子集，返回该声明。

    对不支持的模态声明「结构化产物」没有意义，只能是实现写错；在消费方构造时拒绝，
    不让错误声明静默存在。
    """
    structured = frozenset(normalizer.structured_modalities())
    unsupported = structured - set(normalizer.modalities())
    if unsupported:
        names = sorted(item.value for item in unsupported)
        raise ValidationError(
            f"{type(normalizer).__name__}.structured_modalities() declares {names}, "
            "which are not in modalities()"
        )
    return structured


def ensure_normalizer_supports(normalizer: Normalizer, modality: Modality) -> None:
    """确保 Normalizer 声明支持输入模态，否则报统一能力错误。"""
    if modality in normalizer.modalities():
        return
    raise UnsupportedCapabilityError(
        capability="modality",
        value=modality.value,
        component=type(normalizer).__name__,
    )
