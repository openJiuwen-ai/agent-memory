"""评测断点：把已完成的题目结果原子落盘，供中断后续跑。

设计约束（每条都对应一次真实故障）：

1. **key 用 query_id，不用位置下标**
   位置下标来自「过滤后的列表」，会随 ``--samples`` / ``--max-questions``
   改变；两批不同的题会撞到同一个断点文件，续跑时静默返回上一批的结果。

2. **命中前校验覆盖范围**
   断点里的结果必须恰好覆盖本次请求的 query_id 集合。空断点、缺题、多题
   一律视为未命中重跑——否则会产出 ``n_queries=0`` 的假成功报告。

3. **任何损坏都退回重跑，绝不抛给调用方**
   截断 JSON、非对象 JSON、字段错位、fingerprint 不符：一律返回 ``None``。
   既然这是崩溃恢复功能，它本身不能成为新的崩溃源。

4. **写失败要显式抛出**
   调用方据此决定保留结果（而不是连同已算出的结果一起丢弃）。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from enum import Enum
from pathlib import Path
from uuid import uuid4

from .types import CaseOutcome

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

# 编码/解码需要还原的类型。新增数据类型时在此登记。
_TYPES: dict[str, type] = {"CaseOutcome": CaseOutcome}


class CheckpointError(RuntimeError):
    """断点编码或写入失败。加载失败不抛此异常，一律返回 None。"""


def _register_lazy_types() -> dict[str, type]:
    """TrajectoryStep / RecallChannel 属于内核，惰性导入避免评测层启动即耦合。"""
    if "TrajectoryStep" not in _TYPES:
        from jiuwen_memory.retrieval.types import RecallChannel, TrajectoryStep

        _TYPES.update(TrajectoryStep=TrajectoryStep, RecallChannel=RecallChannel)
    return _TYPES


def _encode_set(obj: set) -> dict[str, object]:
    try:
        ordered = sorted(obj)
    except TypeError:
        # 混合类型不可排序；按 repr 排序仍保持确定性。
        ordered = sorted(obj, key=repr)
    return {"__set__": ordered}


def _encode_dataclass(obj: object) -> dict[str, object]:
    payload: dict[str, object] = {"__dataclass__": type(obj).__name__}
    for field in dataclasses.fields(obj):  # type: ignore[arg-type]
        payload[field.name] = _encode(getattr(obj, field.name))
    return payload


_SCALAR_TYPES = (str, int, float)


def _encode_sequence(obj: list | tuple) -> object:
    encoded = [_encode(item) for item in obj]
    # tuple 需要显式标签，否则解码成 list 会丢类型。
    return {"__tuple__": encoded} if isinstance(obj, tuple) else encoded


def _encode(obj: object) -> object:
    # 标量原样通过（bool 是 int 子类，无需特判）
    if obj is None or isinstance(obj, _SCALAR_TYPES):
        return obj
    if isinstance(obj, set):
        return _encode_set(obj)
    if isinstance(obj, Enum):
        return {"__enum__": type(obj).__name__, "value": obj.value}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _encode_dataclass(obj)
    if isinstance(obj, (list, tuple)):
        return _encode_sequence(obj)
    if isinstance(obj, dict):
        return {key: _encode(value) for key, value in obj.items()}
    raise CheckpointError(f"cannot encode value of type {type(obj).__name__}")



def _decode_enum(name: str, value: object) -> object:
    types = _register_lazy_types()
    if name not in types:
        raise CheckpointError(f"unknown enum {name}")
    return types[name](value)


def _decode_dataclass(name: str, fields: dict[str, object]) -> object:
    types = _register_lazy_types()
    if name not in types:
        raise CheckpointError(f"unknown dataclass {name}")
    kwargs = {k: _decode(v) for k, v in fields.items() if k != "__dataclass__"}
    return types[name](**kwargs)


def _decode_dict(obj: dict[str, object]) -> object:
    """按类型标签还原；无标签时递归处理普通字典。"""
    if "__set__" in obj:
        return set(obj["__set__"])  # type: ignore[arg-type]
    if "__tuple__" in obj:
        return tuple(_decode(item) for item in obj["__tuple__"])  # type: ignore[union-attr]
    if "__enum__" in obj:
        return _decode_enum(str(obj["__enum__"]), obj["value"])
    if "__dataclass__" in obj:
        return _decode_dataclass(str(obj["__dataclass__"]), obj)
    return {key: _decode(value) for key, value in obj.items()}


def _decode(obj: object) -> object:
    if isinstance(obj, list):
        return [_decode(item) for item in obj]
    if isinstance(obj, dict):
        return _decode_dict(obj)
    return obj



def checkpoint_encode(outcome: CaseOutcome) -> dict[str, object]:
    """把一个 CaseOutcome 编码成可 JSON 化的结构。"""
    encoded = _encode(outcome)
    if not isinstance(encoded, dict):
        raise CheckpointError("encoded outcome must be an object")
    return encoded


def checkpoint_decode(payload: object) -> CaseOutcome:
    """还原 CaseOutcome；结构不符时抛 CheckpointError。"""
    decoded = _decode(payload)
    if not isinstance(decoded, CaseOutcome):
        raise CheckpointError("decoded value is not a CaseOutcome")
    return decoded


def fingerprint_of(**fields: object) -> str:
    """把影响结果的全部参数压成一个指纹。

    调用方必须把每个会影响评测结果的参数都传进来；遗漏会让不同配置的断点
    互相命中。键序固定，因此同一组参数总是得到同一指纹。
    """
    canonical = json.dumps(
        {key: str(fields[key]) for key in sorted(fields)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _path_for(directory: Path, query_id: str) -> Path:
    """断点文件按单个 query_id 命名。

    粒度必须是「一道题一个文件」：若按题目集合命名，存了 q0,q1 之后请求
    q0,q1,q2,q3 会落到不同文件，续跑永远命不中（已实测）。
    逐题命名也让并发写者天然分片——不同题写不同文件，无共享可变状态。
    """
    digest = hashlib.sha256(query_id.encode("utf-8")).hexdigest()[:16]
    return directory / f"ckpt_{digest}.json"


def _require_outcomes(outcomes: list[CaseOutcome]) -> None:
    """非空且全部是 CaseOutcome —— 在边界一次校验完。"""
    if not outcomes:
        raise CheckpointError("refusing to save empty checkpoint")
    for outcome in outcomes:
        if not isinstance(outcome, CaseOutcome):
            raise CheckpointError(f"expected CaseOutcome, got {type(outcome).__name__}")


def _write_atomically(target: Path, serialized: str) -> None:
    """写临时文件再原子替换；并发写同一目录时互不覆盖半成品。"""
    temporary = target.with_suffix(f".{uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        raise CheckpointError(f"checkpoint write failed: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def save_checkpoint(
    directory: Path,
    fingerprint: str,
    outcomes: list[CaseOutcome],
) -> None:
    """原子写入。失败抛 CheckpointError，由调用方决定是否保留已算结果。"""
    _require_outcomes(outcomes)
    directory = Path(directory)
    for outcome in outcomes:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "query_id": outcome.query_id,
            "result": checkpoint_encode(outcome),
        }
        try:
            serialized = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise CheckpointError(
                f"checkpoint serialization failed for {outcome.query_id}: {exc}"
            ) from exc
        _write_atomically(_path_for(directory, outcome.query_id), serialized)


def _read_payload(target: Path) -> dict[str, object] | None:
    """读取并解析断点文件；任何读取/解析问题都返回 None。"""
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("checkpoint unreadable (%s), rerunning", exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("checkpoint payload is not an object, rerunning")
        return None
    return payload


def _load_one(
    directory: Path,
    fingerprint: str,
    query_id: str,
    schema_version: int,
) -> CaseOutcome | None:
    """读一道题的断点；任何不满足前提的情况返回 None。"""
    target = _path_for(directory, query_id)
    if not target.is_file():
        return None
    payload = _read_payload(target)
    if payload is None:
        return None
    if payload.get("schema_version") != schema_version:
        logger.warning("checkpoint schema mismatch for %s, rerunning", query_id)
        return None
    if payload.get("fingerprint") != fingerprint:
        logger.warning("checkpoint fingerprint mismatch for %s, rerunning", query_id)
        return None
    if payload.get("query_id") != query_id:
        logger.warning("checkpoint query_id mismatch for %s, rerunning", query_id)
        return None
    try:
        outcome = checkpoint_decode(payload.get("result"))
    except (CheckpointError, TypeError) as exc:
        logger.warning("checkpoint decode failed for %s (%s), rerunning", query_id, exc)
        return None
    if outcome.query_id != query_id:
        logger.warning("checkpoint contents mismatch for %s, rerunning", query_id)
        return None
    return outcome


def load_checkpoint(
    directory: Path,
    fingerprint: str,
    query_ids: list[str],
    *,
    schema_version: int = _SCHEMA_VERSION,
) -> list[CaseOutcome] | None:
    """读取一组题的断点。

    **全部命中才返回**；任何一道缺失或不合格都返回 None，由调用方整批重跑。
    这样调用方的「命中即跳过」不会漏掉未完成的题。
    """
    if not query_ids:
        return None
    directory = Path(directory)
    outcomes: list[CaseOutcome] = []
    for query_id in query_ids:
        outcome = _load_one(directory, fingerprint, query_id, schema_version)
        if outcome is None:
            return None
        outcomes.append(outcome)
    return outcomes

