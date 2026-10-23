# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Entity dedup keys must be stable for Unicode case/full-width variants."""

from jiuwen_memory.common.type_def.normalizer import EntityNormalizer


def test_casefold_makes_sharp_s_and_ss_the_same_key() -> None:
    assert EntityNormalizer.normalize("Straße") == EntityNormalizer.normalize("STRASSE")


def test_nfkc_folds_fullwidth_latin() -> None:
    assert EntityNormalizer.normalize("ＡＢＣ １２３") == "abc 123"
