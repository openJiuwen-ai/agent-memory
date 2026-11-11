from types import SimpleNamespace

import pytest

from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "stage",
    [
        "parse",
        "build_filters",
        "select_channels",
        "recall_and_fuse",
        "rerank",
        "rollup",
        "disclose",
        "expand",
        "finalize",
    ],
)
def test_missing_state(stage):
    with pytest.raises(RuntimeError):
        getattr(PipelineRetriever, stage)(
            None, SimpleNamespace(query=None, hierarchy=None, parsed=None)
        )
