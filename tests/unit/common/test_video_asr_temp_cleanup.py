# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""VideoASR must not delete a caller-supplied temp_work_dir (and its outputs)."""

import json
from pathlib import Path

from jiuwen_memory.common.normalizer.normalizer_impl import video_asr
from jiuwen_memory.common.normalizer.normalizer_impl.video_asr import (
    VideoAsrConfig,
    run_video_asr,
)


class _UnusedService:
    @staticmethod
    def transcribe(*args, **kwargs):
        raise AssertionError("no audio stream should not call ASR")


def test_user_temp_work_dir_and_output_json_survive_cleanup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(video_asr, "_has_audio_stream", lambda _path: False)
    work_dir = tmp_path / "asr-work"
    work_dir.mkdir()
    output_json = work_dir / "asr.json"

    run_video_asr(
        tmp_path / "silent.mp4",
        VideoAsrConfig(
            service=_UnusedService(),
            output_json=output_json,
            temp_work_dir=work_dir,
        ),
    )

    assert output_json.is_file()
    assert json.loads(output_json.read_text(encoding="utf-8")) == []
