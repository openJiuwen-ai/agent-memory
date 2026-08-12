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

from ..factory.factory import Factory
from ..base import Plugin
from ..type_def import Modality, RawPayload


class NormalizerProducer(Factory):
    """Normalizer 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``normalizer_impl`` 下以 ``@NormalizerProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "normalizer"


class Normalizer(Plugin):
    @abstractmethod
    def modalities(self) -> list[Modality]:
        """返回本规约器支持的模态。"""

    @abstractmethod
    def normalize(self, payload: RawPayload) -> str:
        """将原模态负载规约为可治理的文本/结构投影（content）。"""
