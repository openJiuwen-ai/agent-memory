"""Scheduler — 演进调度（架构 §8 双通道）。

控制层驱动构建层自演进的调度面：hot 通道做低时延的在线轻量更新，
background 通道异步做重的抽取/升华/重索引，不阻塞主链路。
控制模式（agent_control / static_control / both）决定由谁触发提交。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import Scope
from construction import EvolveMode

from .base import ControlOperator
from .types import Channel, JobInfo


class Scheduler(ControlOperator):
    @abstractmethod
    def submit(self, scope: Scope, mode: EvolveMode, channel: Channel) -> str:
        """提交一次演进任务（指定阶段与通道），返回任务 id。"""

    @abstractmethod
    def status(self, job_id: str) -> JobInfo:
        """查询任务状态。"""

    @abstractmethod
    def cancel(self, job_id: str) -> None:
        """取消尚未完成的任务（幂等）。"""
