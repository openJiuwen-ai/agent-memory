# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""
Graph store index fields

Provides the base ``VectorField`` configuration class plus the Milvus index
field variants (AUTO/HNSW/IVF/FLAT/SCANN) used by the graph store to configure
Approximate Nearest Neighbour (ANN) search. Supports stage-based field
filtering for construction and search operations.

Self-contained within the graph store: migrated from the original
``foundation/store/vector_fields`` (base + milvus_fields) since the standalone
agent-memory repo has no equivalent ANN index-config abstraction.
"""

from typing import Any, Literal, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_core import PydanticCustomError

DEFAULT = None
IS_SEARCH = {"json_schema_extra": {"stage": "search"}}
IS_CONSTRUCT = {"json_schema_extra": {"stage": "construct"}}


def create_extra_construct_field() -> FieldInfo:
    """Create the pydantic Field for extra_construct"""
    return Field(
        default_factory=dict,
        description="Extra index-building arguments to pass into database during construction",
        **IS_CONSTRUCT,
    )


def create_extra_search_field() -> FieldInfo:
    """Create the pydantic Field for extra_search"""
    return Field(
        default_factory=dict,
        description="Extra index-building arguments to pass into database during search",
        **IS_SEARCH,
    )


class VectorField(BaseModel):
    """Base class for configuring Approximate Nearest Neighbor (ANN) search in vector databases.

    Provides a common interface for configuring vector field indexes across different
    database backends. Supports stage-based field filtering to separate construction
    and search parameters.

    Attributes:
        vector_field: Name of the vector field in the database schema.
        database_type: Type of vector database (milvus or chroma).
        index_type: Type of ANN index algorithm (auto, hnsw, flat, ivf, or scann).
    """

    vector_field: str = Field(default="embedding", description="Vector field name")
    database_type: Literal["milvus", "chroma"] = Field(description="Database type")
    index_type: Literal["auto", "hnsw", "flat", "ivf", "scann"] = Field(description="ANN index type")

    # Disallow extra arguments to stop typo, validate on assignment to prevent user error
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @staticmethod
    def should_keep(finfo: FieldInfo, stage: Literal["search", "construct"]) -> bool:
        """Determine whether a field should be kept for a given stage.

        Fields can be marked with a "stage" in their json_schema_extra to indicate
        they are only relevant for "construct" or "search" operations.

        Args:
            finfo: Pydantic FieldInfo containing field metadata.
            stage: The stage to check: "search" (search-only), or "construct" (construction-only).

        Returns:
            bool: True if the field should be kept for this stage, False otherwise.
        """
        return stage == (finfo.json_schema_extra or {}).get("stage")

    def to_dict(self, stage: Literal["search", "construct"]) -> dict[str, Any]:
        """Convert the vector field configuration to a dictionary for a specific stage.

        Filters fields based on the specified stage and merges extra arguments
        for that stage. Removes internal fields (database_type, index_type, vector_field)
        and stage-specific extra fields that don't match the current stage.

        Args:
            stage: The stage to generate configuration for:
                - "search": Fields and extra_search arguments for search operations
                - "construct": Fields and extra_construct arguments for index construction

        Returns:
            dict[str, Any]: Dictionary containing only the relevant fields for the stage,
                with extra arguments merged in.
        """
        cls = self.__class__
        # Filter model fields by stages they apply to, then filter out None
        model_filtered = (
            (attr, getattr(self, attr, None))
            for attr, finfo in cls.model_fields.items()
            if self.should_keep(finfo, stage)
        )
        model_content = {attr: val for attr, val in model_filtered if val is not None}

        # Unpack the extra arguments for each stage
        for attr in ["database_type", "index_type", "vector_field", "variant"]:
            model_content.pop(attr, None)
        extra = model_content.pop(f"extra_{stage}", {})
        return model_content | extra


class MilvusVectorField(VectorField):
    """Base class for Milvus vector field configurations.

    Not intended for direct use. Use specific index classes like:
    MilvusAUTO, MilvusHNSW, MilvusIVF, MilvusFLAT, or MilvusSCANN instead.
    """

    database_type: Literal["milvus"] = Field(default="milvus", description="Database type", init=False)
    index_type: Literal["auto", "hnsw", "flat", "ivf", "scann"] = Field(description="ANN index type", init=False)

    @staticmethod
    def validate_sq_construct(extra_construct: dict[str, Any]) -> str:
        """Validate scalar quantization (SQ) options for index construction.

        Args:
            extra_construct: Dictionary containing extra construction parameters.
                Expected keys:
                - refine (bool): Whether to enable refinement
                - refine_type (str, optional): Type of refinement, must be one of
                  ["SQ6", "SQ8", "FP16", "BF16", "FP32"] if refine is True

        Returns:
            str: Empty string if validation passes, otherwise error message
                starting with semicolon-separated issues.
        """
        err_msg = ""
        if extra_construct:
            refine = extra_construct.get("refine", False)
            refine_type = extra_construct.get("refine_type")
            if not isinstance(refine, bool):
                err_msg += '; "refine" must be a bool value'
            if refine is True and refine_type not in [None, "SQ6", "SQ8", "FP16", "BF16", "FP32"]:
                err_msg += '; if set, "refine_type" must be one of ["SQ6", "SQ8", "FP16", "BF16", "FP32"]'
        return err_msg

    @staticmethod
    def validate_sq_search(extra_search: dict[str, Any]) -> str:
        """Validate scalar quantization (SQ) options for search stage.

        Args:
            extra_search: Dictionary containing extra search parameters.
                Expected keys:
                - refine_k (float, optional): Refinement factor, must be >= 1

        Returns:
            str: Empty string if validation passes, otherwise error message
                starting with semicolon-separated issues.
        """
        err_msg = ""
        if extra_search:
            refine_k = extra_search.get("refine_k", 1.0)
            if not (isinstance(refine_k, (int, float)) and refine_k >= 1):
                err_msg += '; "refine_k" must be float >= 1'
        return err_msg

    @staticmethod
    def validate_pq_construct(extra_construct: dict[str, Any]) -> str:
        """Validate product quantization (PQ) options for index construction.

        Args:
            extra_construct: Dictionary containing extra construction parameters.
                Expected keys:
                - m (int, optional): Number of sub-vectors, must be in range [1, 65536]
                - nbits (int, optional): Number of bits per sub-vector, must be in
                  range [1, 24]. Defaults to 8.

        Returns:
            str: Empty string if validation passes, otherwise error message
                starting with semicolon-separated issues.
        """
        err_msg = ""
        if extra_construct:
            m = extra_construct.get("m")
            nbits = extra_construct.get("nbits", 8)
            if not (m is None or (isinstance(m, int) and (1 <= m <= 65536))):
                err_msg += '; "m" must be either None or int in range [1, 65536]'
            if not (isinstance(nbits, int) and (1 <= nbits <= 24)):
                err_msg += '; "nbits" must be int in range [1, 24]'
        return err_msg


class _BaseIVF(MilvusVectorField):
    """Base class for IVF-based index configurations.

    Not intended for direct use. Use MilvusIVF or MilvusSCANN instead.
    """

    index_type: Literal["ivf"] = Field(default="ivf", description="ANN index type", init=False)
    nlist: int = Field(
        default=128,
        ge=1,
        le=65536,
        description="Number of clusters to create by k-means algorithm during construction.",
        **IS_CONSTRUCT,
    )
    nprobe: int = Field(
        default=8,
        ge=1,
        le=65536,
        description="Controls the amount of clusters searched, expand search scope at the cost of latency.",
        **IS_SEARCH,
    )

    @model_validator(mode="after")
    def validate_nprobe_and_extra_args(self) -> Self:
        """Validate that nprobe is not greater than nlist.

        Returns:
            Self: The validated instance.

        Raises:
            PydanticCustomError: If nprobe > nlist.
        """
        if self.nprobe > self.nlist:
            raise PydanticCustomError(
                "nprobe_vs_nlist",
                "nprobe must be <= nlist (got nprobe={nprobe}, nlist={nlist})",
                {"nprobe": self.nprobe, "nlist": self.nlist},
            )
        return self


class MilvusFLAT(MilvusVectorField):
    """FLAT index configuration for Milvus.

    FLAT index performs exact nearest neighbor search without approximation.
    Provides the highest accuracy but with higher memory usage and slower search
    compared to approximate methods. Suitable for small to medium-sized datasets.
    """

    index_type: Literal["flat"] = Field(default="flat", description="ANN index type", init=False)


class MilvusAUTO(MilvusVectorField):
    """AUTOINDEX configuration for Milvus.

    AUTOINDEX is the default index type in Milvus, providing good balance between performance and ease of use.

    Configurable in milvus.yaml when deploying Milvus database.
    Defaults to {"M": 18,"efConstruction": 240,"index_type": "HNSW", "metric_type": "COSINE"}
    """

    index_type: Literal["auto"] = Field(default="auto", description="ANN index type", init=False)


class MilvusSCANN(_BaseIVF):
    """SCANN (Scalable Nearest Neighbors) index configuration for Milvus.

    SCANN is an IVF-based index that uses product quantization for compression.
    It provides a good balance between search speed, accuracy, and memory usage,
    making it suitable for large-scale deployments.

    Inherits IVF clustering parameters (nlist, nprobe) from MilvusIVFBase.

    Attributes:
        nlist: Number of clusters to create during index construction.
            Higher values improve accuracy but increase construction time.
            Default: 128, Range: [1, 65536]
        nprobe: Number of clusters to search during query time. Must be <= nlist.
            Higher values improve recall at the cost of latency.
            Default: 8, Range: [1, 65536]
        with_raw_data: Whether to store original vectors. Setting to True improves
            accuracy at the cost of increased storage. Default: True
        reorder_k: Number of results to reorder using higher precision vectors during search.
            Only effective when with_raw_data is True. Higher values improve accuracy
            but increase latency. Default: None (no reordering)
    """

    index_type: Literal["scann"] = Field(default="scann", description="ANN index type", init=False)
    with_raw_data: bool = Field(
        default=True,
        description="Store original data, increase accuracy at the cost of storage.",
        **IS_CONSTRUCT,
    )
    reorder_k: int = Field(
        default=DEFAULT,
        ge=1,
        description="Number of results to reorder using higher precision vectors during search. "
        "Only effective when with_raw_data is True. Higher values improve accuracy at the cost of latency.",
        **IS_SEARCH,
    )


class MilvusIVF(_BaseIVF):
    """Inverted File (IVF) index configuration for Milvus.

    IVF divides the vector space into clusters using k-means algorithm and
    searches only the most relevant clusters during query time. This provides
    a good balance between search speed and accuracy.

    Supports different quantization variants to further optimize memory usage:
    - FLAT: No quantization, highest accuracy but highest memory usage
    - SQ8: Scalar quantization with 8-bit precision, reduces memory by ~75%
    - PQ: Product quantization, configurable compression via m and nbits
    - RABITQ: Residual-aware binary quantization, best compression

    Attributes:
        nlist: Number of clusters to create during index construction.
            Higher values improve accuracy but increase construction time.
            Default: 128, Range: [1, 65536]
        nprobe: Number of clusters to search during query time. Must be <= nlist.
            Higher values improve recall at the cost of latency.
            Default: 8, Range: [1, 65536]
        variant: The quantization variant to use. Determines which extra arguments
            are valid for construction and search stages. Default: "FLAT"
        extra_construct: Additional parameters for index construction, validated
            based on the selected variant. See validate_extra_args for details.
        extra_search: Additional parameters for search, validated based on the
            selected variant. See validate_extra_args for details.
    """

    variant: Literal["FLAT", "SQ8", "PQ", "RABITQ"] = Field(default="FLAT", description="IVF variants")
    extra_construct: dict[str, Any] = create_extra_construct_field()
    extra_search: dict[str, Any] = create_extra_search_field()

    @model_validator(mode="after")
    def validate_extra_args(self) -> Self:
        """Validate extra_construct and extra_search parameters based on variant.

        Validates that the extra arguments are appropriate for the selected
        variant type:
        - FLAT/SQ8: No extra arguments allowed
        - PQ: Only extra_construct allowed (m, nbits)
        - RABITQ: Both extra_construct (refine, refine_type) and extra_search
          (refine_k, rbq_query_bits) allowed

        Returns:
            Self: The validated instance.

        Raises:
            PydanticCustomError: If extra arguments are invalid for the variant.
        """
        # Check extra_construct and extra_search have correct settings
        extra_construct: dict[str, Any] = getattr(self, "extra_construct")
        extra_search: dict[str, Any] = getattr(self, "extra_search")
        error_context = {"extra_construct": extra_construct, "extra_search": extra_search}
        err_msg = ""
        match self.variant:
            case "FLAT" | "SQ8":
                if extra_construct or extra_search:
                    err_msg += f"{self.variant} does not accept any extra arguments"
            case "PQ":
                err_msg += self.validate_pq_construct(extra_construct)
                if extra_search:
                    err_msg += "; this variant does not accept extra search arguments"
            case "RABITQ":
                if extra_construct:
                    err_msg += self.validate_sq_construct(extra_construct)
                if extra_search:
                    err_msg += self.validate_sq_search(extra_search)
                    rbq_query_bits = extra_search.get("rbq_query_bits", 0)
                    if not (isinstance(rbq_query_bits, int) and (0 <= rbq_query_bits <= 8)):
                        err_msg += '; "rbq_query_bits" must be int in range [0, 8]'
        if err_msg:
            err_msg = err_msg.removeprefix("; ")
            raise PydanticCustomError(
                "invalid_extra_args",
                f"MilvusIVF with {self.variant} variant has invalid extra arguments: {err_msg}",
                error_context,
            )
        return self


class MilvusHNSW(MilvusVectorField):
    """Hierarchical Navigable Small World (HNSW) index configuration for Milvus.

    HNSW builds a multi-layer graph structure for efficient approximate nearest
    neighbor search. It provides excellent search performance and accuracy,
    especially for high-dimensional vectors. Generally recommended for most use cases.

    Supports optional quantization variants to reduce memory usage:
    - SQ: Scalar quantization, reduces memory by ~75% with minimal accuracy loss
    - PQ: Product quantization, configurable compression via m and nbits
    - PRQ: Product residual quantization, best compression ratio

    Attributes:
        M: Maximum number of edges per node in the graph. Higher
            values improve accuracy but increase memory usage and construction time.
            Default: 30, Range: [2, 2048]
        efConstruction: Number of candidate neighbors to consider during index
            construction. Higher values improve graph quality but slow construction.
            Default: 360, Range: [1, ∞)
        efSearchFactor: Multiplier for search breadth. If set, ef = top_k * efSearchFactor.
            Higher values improve recall at the cost of latency. Default: None
        variant: Optional quantization variant to reduce memory usage.
            Options: "SQ", "PQ", "PRQ", or None (no quantization). Default: None
        extra_construct: Additional parameters for index construction, validated
            based on the selected variant. See validate_extra_args for details.
        extra_search: Additional parameters for search, validated based on the
            selected variant. See validate_extra_args for details.
    """

    index_type: Literal["hnsw"] = Field(default="hnsw", description="ANN index type", init=False)
    M: int = Field(
        default=30,
        ge=2,
        le=2048,
        description="Maximum number of edges each node can have in the graph",
        **IS_CONSTRUCT,
    )
    efConstruction: int = Field(
        default=360,
        ge=1,
        description="Number of candidate neighbors considered during index construction",
        **IS_CONSTRUCT,
    )
    efSearchFactor: float = Field(
        default=DEFAULT,
        ge=1,
        description="Controls the search breadth for Milvus HNSW search queries. "
        "If set, top_k * efSearchFactor = ef (in Milvus) = number of candidates explored during search. "
        "High efSearchFactor improves recall at the cost of latency.",
        **IS_SEARCH,
    )
    variant: Optional[Literal["SQ", "PQ", "PRQ"]] = Field(
        default=DEFAULT,
        description="HNSW variants supported by Milvus, using different quantization techniques.",
    )
    extra_construct: dict[str, Any] = create_extra_construct_field()
    extra_search: dict[str, Any] = create_extra_search_field()

    @model_validator(mode="after")
    def validate_extra_args(self) -> Self:
        """Validate extra_construct and extra_search parameters based on variant.

        Validates that the extra arguments are appropriate for the selected
        variant type:
        - SQ: extra_construct may contain sq_type, refine, refine_type
        - PQ: extra_construct may contain m, nbits; extra_search may contain refine_k
        - PRQ: extra_construct may contain m, nbits, nrq, refine, refine_type;
          extra_search may contain refine_k

        Returns:
            Self: The validated instance.

        Raises:
            PydanticCustomError: If extra arguments are invalid for the variant.
        """
        # Check extra_construct and extra_search have correct settings
        extra_construct: dict[str, Any] = getattr(self, "extra_construct")
        extra_search: dict[str, Any] = getattr(self, "extra_search")
        error_context = {"extra_construct": extra_construct, "extra_search": extra_search}
        err_msg = ""
        match self.variant:
            case "SQ":
                sq_type = extra_construct.get("sq_type", "SQ8")
                if sq_type not in ["SQ4U", "SQ6", "SQ8", "FP16", "BF16"]:
                    err_msg += '; "sq_type" must be one of ["SQ4U", "SQ6", "SQ8", "FP16", "BF16"]'
                err_msg += self.validate_sq_construct(extra_construct)
            case "PQ":
                err_msg += self.validate_pq_construct(extra_construct)
                err_msg += self.validate_sq_construct(extra_construct)
                err_msg += self.validate_sq_search(extra_search)
            case "PRQ":
                nrq = extra_construct.get("nrq", 2)
                err_msg += self.validate_pq_construct(extra_construct)
                if not (isinstance(nrq, int) and 1 <= nrq <= 16):
                    err_msg += '; "nrq" must be int in range [1, 16]'
                err_msg += self.validate_sq_construct(extra_construct)
                err_msg += self.validate_sq_search(extra_search)
        if err_msg:
            err_msg = err_msg.removeprefix("; ")
            raise PydanticCustomError(
                "invalid_extra_args",
                f"MilvusHNSW with {self.variant} variant has invalid extra arguments: {err_msg}",
                error_context,
            )
        return self
