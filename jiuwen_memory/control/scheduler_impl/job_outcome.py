# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""两个 Scheduler 共用的任务终态校验。"""

from jiuwen_memory.control.types import JobInfo, JobStatus


def terminal_job_status(result: JobInfo) -> JobStatus:
    """正常返回也可能代表部分失败；禁止把非终态结果冒充完成。"""
    if not isinstance(result.status, JobStatus) or result.status not in (
        JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED,
    ):
        raise ValueError(f"Job.run() must return a terminal status, got {result.status!r}")
    return result.status
