"""Derivation build recovery: frozen plan, output keys, per-item reconciliation.

An external event produces derived memories (extraction / dedup / consolidation). Because a
derived write may land while its receipt is lost, recovery replays a *frozen* plan item by
item against two write-chain capabilities:

- **read-after-write exact query** by ``system_metadata.continuity_output`` (``query_output``),
  used for ``created`` outputs; without it ``created`` reconciliation is unreliable;
- **atomic conditional update** (``conditional_update``) with an expected digest, used for
  ``updated`` outputs; without it ``updated`` actions are never auto-applied and must be
  reported as a partial conflict instead of degrading to read-then-write.

The plan is frozen before any derived write: the same ``build_id`` never regenerates a second
plan, and outputs carry a deterministic ``output_key`` so a written-but-unrecorded result can
be located and adopted instead of duplicated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace

from jiuwen_memory.common.errors import AgentMemoryError, ConflictError
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.common.type_def.continuity import MemoryRef


@dataclass(frozen=True)
class DerivationItem:
    """One frozen output; every action carries the checksums needed to reconcile it."""

    output_key: str
    action: str  # created / reused / updated
    ref: MemoryRef | None = None  # target for reused / updated
    event_key: str = ""  # reused: the source event key for the source association
    content: str = ""  # created / updated: the planned final content
    content_digest: str = ""  # created: planned unit digest (for adopt-or-conflict)
    digest_before: str = ""  # updated: digest expected before applying
    digest_after: str = ""  # updated: digest expected after applying

    def __post_init__(self) -> None:
        if self.action not in {"created", "reused", "updated"}:
            raise ValueError(f"unknown derivation action {self.action!r}")


@dataclass
class DerivationPlan:
    """A frozen build plan keyed by build_id; items are applied in order, idempotently."""

    build_id: str
    items: list[DerivationItem] = field(default_factory=list)


@dataclass
class DerivationResult:
    status: str  # committed / partial
    derived: list[dict] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


# -- capability seams --------------------------------------------------------- #

QueryOutput = Callable[[str], list[MemoryUnit]]
ReadUnit = Callable[[MemoryRef], MemoryUnit]
CreateOutput = Callable[[DerivationItem], MemoryUnit]
AddSource = Callable[[str, str], None]
ConditionalUpdate = Callable[[MemoryRef, str, str, dict[str, str]], bool]


def reconcile(
    plan: DerivationPlan,
    *,
    query_output: QueryOutput | None,
    read: ReadUnit,
    create: CreateOutput,
    add_source: AddSource,
    conditional_update: ConditionalUpdate | None,
) -> DerivationResult:
    """Reconcile a frozen plan against the write chain, item by item.

    Missing capabilities fail closed: without ``query_output`` every ``created`` item is a
    conflict (the adoption check cannot run reliably); without ``conditional_update`` every
    ``updated`` item is a conflict (never degrade to read-then-write).

    Returns ``committed`` only when every item is adopted, reused, or applied without a
    conflict; any unreadable dependency, digest mismatch, rejected CAS, or missing
    capability makes ``partial`` and leaves the conflicting output key in ``conflicts``.
    """
    derived: list[dict] = []
    conflicts: list[str] = []
    partial = False

    for item in plan.items:
        if item.action == "created":
            if query_output is None:
                conflicts.append(item.output_key)
                partial = True
                continue
            existing = query_output(item.output_key)
            if len(existing) > 1:
                conflicts.append(item.output_key)
                partial = True
                continue
            if existing:
                ref = MemoryRef.of(existing[0])
                if ref.content_digest == item.content_digest:
                    # Written earlier but the receipt was lost: adopt, never duplicate.
                    derived.append(
                        {"output_key": item.output_key, "action": "created", "ref": asdict(ref)}
                    )
                else:
                    conflicts.append(item.output_key)
                    partial = True
            else:
                unit = create(item)
                if MemoryRef.of(unit).content_digest != item.content_digest:
                    conflicts.append(item.output_key)
                    partial = True
                    continue
                derived.append(
                    {
                        "output_key": item.output_key,
                        "action": "created",
                        "ref": asdict(MemoryRef.of(unit)),
                    }
                )
            continue

        if item.action == "reused":
            try:
                current = read(item.ref)
                if MemoryRef.of(current) != item.ref:
                    conflicts.append(item.output_key)
                    partial = True
                    continue
            except (AgentMemoryError, KeyError):
                conflicts.append(item.output_key)
                partial = True
                continue
            # The source association is set semantics keyed by (event, reused memory):
            # appending twice is a no-op and never inflates the independent-source count.
            add_source(item.event_key, item.ref.unit_id)
            derived.append(
                {"output_key": item.output_key, "action": "reused", "ref": asdict(item.ref)}
            )
            continue

        # Updated content and batch identity must land in the same conditional write.
        metadata = {"continuity_build": plan.build_id, "continuity_output": item.output_key}
        # updated
        try:
            try:
                current_ref = MemoryRef.of(read(item.ref))
            except ConflictError:
                # An authorized snapshot reader rejects the pre-update digest after
                # a successful write; only the exact frozen post-update digest is valid.
                current_ref = MemoryRef.of(read(replace(
                    item.ref, content_digest=item.digest_after
                )))
        except (AgentMemoryError, KeyError):
            conflicts.append(item.output_key)
            partial = True
            continue
        current_digest = current_ref.content_digest
        if current_digest == item.digest_after:
            # A previous write may have committed truth and then failed projecting it.
            # Reapply identical content with CAS to repair projections without overwriting
            # an intervening change. No model call is repeated.
            # Matching body alone does not prove summaries and batch tags were repaired.
            if conditional_update is None or not conditional_update(
                current_ref, item.digest_after, item.content, metadata
            ):
                conflicts.append(item.output_key)
                partial = True
                continue
            derived.append(
                {
                    "output_key": item.output_key,
                    "action": "updated",
                    "ref": asdict(current_ref),
                    "already_applied": True,
                }
            )
        elif current_digest == item.digest_before:
            if conditional_update is None:
                conflicts.append(item.output_key)
                partial = True
            elif conditional_update(item.ref, item.digest_before, item.content, metadata):
                applied = MemoryRef.of(read(replace(item.ref, content_digest=item.digest_after)))
                if applied.content_digest != item.digest_after:
                    conflicts.append(item.output_key)
                    partial = True
                else:
                    derived.append(
                        {"output_key": item.output_key, "action": "updated", "ref": asdict(applied)}
                    )
            else:
                conflicts.append(item.output_key)
                partial = True
        else:
            # A third party changed the target; never overwrite it.
            conflicts.append(item.output_key)
            partial = True

    status = "partial" if partial else "committed"
    return DerivationResult(status=status, derived=derived, conflicts=conflicts)
