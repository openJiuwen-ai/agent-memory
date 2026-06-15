"""接入层（A 层数据接入侧，架构 §10/§10.1）算子基类。

接入层承接多模态信息源，对每条原始数据做两件事（检索链路不感知模态）：

1. **保留原模态资产引用**——记入 ``MemoryUnit.assets``；
2. **规约出可治理文本/结构投影**——调用 ``src/common`` 的
   :class:`~common.normalizer.Normalizer` 产出 ``content``。

**什么是「规约投影」**：把各种格式的来源统一转成一份系统能处理的文字
描述。「规约」= 统一格式——来源五花八门（对话/PDF/代码/图片/录音/视频），
下游只认文本，所以要把它们都翻译成统一形态；「投影」= 翻译出来的文字
不是原件本身，而是原件在文本世界里的影子——原件留在 ``assets``，影子
存入 ``content``。各模态的翻译方式：

- 图片  → 图片描述（caption）+ 图中文字（OCR）
- 音频  → 语音转写文字稿（ASR）
- 视频  → 关键画面描述 + 字幕/转录
- 文档/代码 → 解析出的正文
- 对话  → 基本为原文，做整理

这么做的收益：下游分词/向量化/建索引/检索**只处理 content 这一份文字**，
不必关心记忆原来是图还是录音，检索链路保持简单一致；且换了更好的转写
模型后，拿原件重跑一遍 Normalizer 即可重建投影（投影可重建，原件不丢）。

随后把规约结果转换为 :class:`~common.type_def.MemoryUnit` 返回。
**接入层不负责落盘**：记忆单元（含资产）写入真源由构建层调用
``src/storage`` 完成。算子拆分：

- :class:`~ingest.source.Source` 信息源连接器（对话/文档/代码/工具轨迹/图像/音视频）
- :class:`~ingest.ingestor.Ingestor` 接入编排（规约 + 转换为记忆单元）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class IngestOperatorType(str, Enum):
    SOURCE = "source"
    INGESTOR = "ingestor"


class IngestOperator(ABC):
    """所有接入层算子的自描述契约。"""

    @abstractmethod
    def operator_type(self) -> IngestOperatorType:
        """返回本算子的类型。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。"""
