"""Entity Schema 加载与生成期过滤。

数据形态兼容 MindMemOS ``EntityManager`` 当前使用的 schema JSON：根节点既可为
entity type 数组，也可为 ``{"entity_types": [...]}``；属性定义保留原始 dict，
以便 prompt 完整携带 ``desc/example/order`` 等扩展字段。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jiuwen_memory.common.errors import ValidationError


@dataclass(frozen=True)
class EntityProperty:
    """一个结构化实体属性定义。"""

    description: str = ""
    examples: list[str] = field(default_factory=list)
    constraints: str = ""
    value_type: str = "string"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityProperty:
        examples = data.get("examples")
        if not isinstance(examples, list):
            example = data.get("example")
            examples = [str(example)] if example else []
        return cls(
            description=str(data.get("description") or data.get("desc") or ""),
            examples=[str(item) for item in examples],
            constraints=str(data.get("constraints") or ""),
            value_type=str(data.get("value_type") or data.get("type") or "string"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EntityType:
    """一个 entity type 的 schema。"""

    entity_type: str
    entity_description: str = ""
    entity_instruction: str = ""
    search_weight: float = 1.0
    static_property: dict[str, Any] = field(default_factory=dict)
    dynamic_property: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityType:
        entity_type = str(data.get("entity_type") or "").strip()
        if not entity_type:
            raise ValidationError("Entity Schema 的 entity_type 不能为空")
        static_property = data.get("static_property") or {}
        dynamic_property = data.get("dynamic_property") or {}
        if not isinstance(static_property, dict) or not isinstance(dynamic_property, dict):
            raise ValidationError(
                f"Entity Schema {entity_type!r} 的 static_property/dynamic_property 必须是对象"
            )
        try:
            search_weight = float(data.get("search_weight", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"Entity Schema {entity_type!r} 的 search_weight 必须是数字"
            ) from exc
        return cls(
            entity_type=entity_type,
            entity_description=str(data.get("entity_description") or ""),
            entity_instruction=str(data.get("entity_instruction") or ""),
            search_weight=search_weight,
            static_property=copy.deepcopy(static_property),
            dynamic_property=copy.deepcopy(dynamic_property),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_description": self.entity_description,
            "entity_instruction": self.entity_instruction,
            "search_weight": self.search_weight,
            "static_property": copy.deepcopy(self.static_property),
            "dynamic_property": copy.deepcopy(self.dynamic_property),
        }

    def all_property_names(self) -> set[str]:
        return set(self.static_property) | set(self.dynamic_property)


class EntitySchemaCatalog:
    """文件或内存数据支持的 Entity Schema 目录。"""

    def __init__(self, entities: list[EntityType], *, schema_name: str) -> None:
        if not entities:
            raise ValidationError("Entity Schema 至少需要一个 entity type")
        self.schema_name = schema_name
        self._entities: dict[str, EntityType] = {}
        for entity in entities:
            if entity.entity_type in self._entities:
                raise ValidationError(f"Entity Schema entity_type 重复：{entity.entity_type!r}")
            self._entities[entity.entity_type] = entity
        canonical = json.dumps(
            self.get_all_dicts(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.schema_version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_file(cls, file_path: str | Path) -> EntitySchemaCatalog:
        path = _resolve_schema_path(file_path)
        if not path.is_file():
            raise ValidationError(f"Entity Schema 文件不存在：{path}")
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Entity Schema 文件无法读取：{path}: {exc}") from exc
        return cls.from_data(data, schema_name=path.name)

    @classmethod
    def from_data(cls, data: Any, *, schema_name: str = "inline-schema") -> EntitySchemaCatalog:
        if isinstance(data, dict):
            raw_entities = data.get("entity_types")
        elif isinstance(data, list):
            raw_entities = data
        else:
            raw_entities = None
        if not isinstance(raw_entities, list):
            raise ValidationError("Entity Schema 根必须是数组或包含 entity_types 数组的对象")
        entities: list[EntityType] = []
        for index, item in enumerate(raw_entities):
            if not isinstance(item, dict):
                raise ValidationError(f"Entity Schema 第 {index} 项必须是对象")
            entities.append(EntityType.from_dict(item))
        return cls(entities, schema_name=schema_name)

    def get(self, entity_type: str) -> EntityType | None:
        return self._entities.get(entity_type)

    def list_types(self) -> list[str]:
        return list(self._entities)

    def get_all_dicts(self) -> list[dict[str, Any]]:
        return [entity.to_dict() for entity in self._entities.values()]

    def schema_for_generation(self) -> list[dict[str, Any]]:
        """过滤 episodes 与 ``order >= 2`` 属性，得到当前抽取 schema。"""
        schema = copy.deepcopy(self.get_all_dicts())
        filtered = [item for item in schema if item.get("entity_type") != "episodes"]
        for item in filtered:
            dynamic = item.get("dynamic_property", {})
            if isinstance(dynamic, dict):
                item["dynamic_property"] = {
                    name: definition
                    for name, definition in dynamic.items()
                    if not isinstance(definition, dict) or definition.get("order", 1) < 2
                }
        return filtered

    @staticmethod
    def filter_selected(
        full_schema: list[dict[str, Any]],
        selected: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按 MindMemOS schema-selection 结果过滤，并始终保留 default_property。"""
        selected_map: dict[str, list[str]] = {}
        for item in selected:
            if not isinstance(item, dict):
                continue
            entity_type = str(item.get("entity_type") or "").strip()
            if not entity_type:
                continue
            properties = item.get("relevant_properties") or ["all"]
            selected_map[entity_type] = (
                [str(prop) for prop in properties] if isinstance(properties, list) else ["all"]
            )

        filtered: list[dict[str, Any]] = []
        for entity in full_schema:
            entity_type = str(entity.get("entity_type") or "")
            if entity_type not in selected_map:
                continue
            properties = selected_map[entity_type]
            entity_copy = copy.deepcopy(entity)
            if properties != ["all"]:
                dynamic = entity_copy.get("dynamic_property", {})
                if isinstance(dynamic, dict):
                    selected_dynamic: dict[str, Any] = {}
                    if "default_property" in dynamic:
                        selected_dynamic["default_property"] = dynamic["default_property"]
                    for property_name in properties:
                        if property_name in dynamic:
                            selected_dynamic[property_name] = dynamic[property_name]
                    entity_copy["dynamic_property"] = selected_dynamic
            filtered.append(entity_copy)
        return filtered


def _resolve_schema_path(file_path: str | Path) -> Path:
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / path
    return path.resolve()
