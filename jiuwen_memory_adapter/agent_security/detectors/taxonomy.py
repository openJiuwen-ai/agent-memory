"""检测专用三级分类；不携带 SDK 写入、还原、留存或共享策略。"""

from collections.abc import Iterable
from dataclasses import dataclass

from .._validation import has_exact_type


@dataclass(frozen=True)
class DetectionLevel:
    rank: int


class SensitiveTaxonomy:
    def __init__(self, levels=None):
        expected = {"S1": 1, "S2": 2, "S3": 3}
        if levels is not None:
            if not has_exact_type(levels, list):
                raise ValueError("invalid detector levels")
            actual = {}
            for item in levels:
                if not has_exact_type(item, dict) or set(item) - {
                    "name",
                    "rank",
                    "label",
                    "description",
                }:
                    raise ValueError("detector levels do not accept memory write/reveal policies")
                if not has_exact_type(item.get("rank"), int) or item.get("name") in actual:
                    raise ValueError("invalid detector level")
                actual[item.get("name")] = item["rank"]
            if actual != expected:
                raise ValueError("detector requires S1/S2/S3 ranks 1/2/3")
        self.levels = {key: DetectionLevel(rank) for key, rank in expected.items()}

    def max_level(self, names: Iterable[str]) -> str:
        values = set(names) or {"S1"}
        if not values <= self.levels.keys():
            raise ValueError("unknown detector level")
        return max(values, key=lambda name: self.levels[name].rank)
