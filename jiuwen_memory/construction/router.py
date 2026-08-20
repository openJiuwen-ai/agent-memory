"""Router — 归属判定（架构 §6，规约见 docs/specs/S09-collective-memory.md）。

逐条决定派生记忆落哪个空间、打哪些收窄维标签。与 :class:`~construction.classifier.Classifier`、
:class:`~construction.layer_annotator.LayerAnnotator` 同构——契约在接口层、实现自注册、
不注入即整段跳过。

本模块除契约外还承载三样东西：

- **判定表数据类**：``MemoryClass`` / ``NarrowDim`` / ``SpaceNaming`` / ``RouteTable``，
  由 ``router`` 配置命名空间在装配期解析产出（:func:`parse_route_table`），加载期十三条
  校验任一不过即装配失败；
- **判定的输入与输出**：``RouteContext`` / ``RouteDecision``；
- **两个落盘不变量**：:func:`enforce_sanitized` 与 :func:`with_all_tag_keys`。两者不放进
  任何 ``Router`` 实现内部——放实现内则换一个实现即可能漏掉，而漏掉的失效方向都是放行
  或静默收窄。

判定表落构建层而非 API 层，因为其输入类型定义在该层；API 层引用构建层类型是既有形态，
依赖方向为 API → 构建，无环。
"""

from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.common.type_def.memory import (
    KERNEL_SYSTEM_METADATA_KEYS,
    MEMORY_CLASS_KEY,
    ROUTE_CTX_KEY,
)

from .base import ConstructionOperator

logger = get_logger(__name__)

# 内核自带的归属坐标实体名。取值以调用方身份为准，部署声明项不得与之重名——
# 重名即接入方传入的取值覆盖内核由身份推导的权威取值（加载期第 12 条校验）。
KERNEL_COORD_KEYS: tuple[str, ...] = ("user", "agent", "session")

# 空间名模板里的占位符形态：``{key}``。
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


# ====================================================================== #
# 判定表数据类
# ====================================================================== #


@dataclass(frozen=True)
class MemoryClass:
    """一个归属类别：这一类记忆归谁、落哪个空间。

    ``owner`` 是归属维实体名（如 ``user`` / ``project``），``space_template`` 是该类别的
    空间名模板。``record_only`` 的类别不落独立空间——它的归属实体只记成一个标签键
    （``tag_key``），命中该类别的条目仍落 fallback 空间。agent-team 属此列：team 被多用户
    共用、本身不是可见性边界，做成空间会导致换 team 失忆。

    ``members`` 与 ``sanitized`` 首版无运行时使用点但保留：两者是加载期第 2 条校验的输入，
    删掉即等于删掉「``cross_user`` 类别不得对全组织敞开」这条防护。
    """

    name: str
    description: str = ""
    owner: str = ""
    space_template: str = ""
    cross_user: bool = False
    record_only: bool = False
    members: str = ""
    sanitized: bool = False
    fallback: bool = False
    tag_key: str = ""


@dataclass(frozen=True)
class NarrowDim:
    """一个收窄维：这一维参与过滤，写入时落成条目标签、检索时折算为过滤条件。

    与类别正交，各为独立的是非题：并入类别枚举时类别数为 2^n，拆开后为 n + m。
    ``applies_to`` 为空即对全部类别生效。
    """

    entity: str
    tag_key: str
    applies_to: tuple[str, ...] = ()
    question: str = ""

    def applies(self, memory_class: str) -> bool:
        return not self.applies_to or memory_class in self.applies_to


@dataclass(frozen=True)
class SpaceNaming:
    """归属坐标到空间名的映射，由类别声明渲染。

    候选空间集合与 fallback 空间共用同一份映射：两处各写一份渲染规则时，候选集算出的
    空间名与兜底落点可以不同，表现为写入落到预期之外的空间且不报错。

    与判定表同源、同一次解析产出，不单独配置也不进工厂。
    """

    templates: tuple[tuple[str, str], ...] = ()
    fallback_class: str = ""

    def template_for(self, memory_class: str) -> str:
        for name, template in self.templates:
            if name == memory_class:
                return template
        return ""

    def render(self, memory_class: str, coords: Mapping[str, str]) -> str:
        """渲染该类别的空间名；模板缺失或坐标缺项时返回空串。

        返回空串而不是抛错：坐标缺项是常态（一次调用未必落在每个实体上下文里），
        表现为该类别不进候选集，而不是整次写入失败。
        """
        return _render_template(self.template_for(memory_class), coords)

    def spaces(self, coords: Mapping[str, str]) -> dict[str, str]:
        """本次坐标可渲染出的全部空间名，返回 ``类别名 -> 空间名``（保序）。"""
        rendered: dict[str, str] = {}
        for name, template in self.templates:
            space = _render_template(template, coords)
            if space:
                rendered[name] = space
        return rendered

    def fallback_space(self, coords: Mapping[str, str]) -> str:
        return self.render(self.fallback_class, coords)


@dataclass(frozen=True)
class RouteTable:
    """判定表的解析产物，四样各有确定使用点。

    | 产物 | 使用点 |
    |---|---|
    | ``classes`` / ``narrow_dims`` | 判定上下文与检索侧的坐标折算 |
    | ``tag_keys`` | 写入边界拒绝调用方占用 |
    | ``naming`` | 候选空间集合与 fallback 空间 |

    未声明 ``router`` 命名空间时取 :data:`EMPTY_ROUTE_TABLE`：四样均为空，写入侧
    ``scope`` 必填、判定路径不可达，全链路行为与未启用该特性一致——这是可灰度上线的前提。
    """

    classes: tuple[MemoryClass, ...] = ()
    narrow_dims: tuple[NarrowDim, ...] = ()
    tag_keys: frozenset[str] = frozenset()
    naming: SpaceNaming = SpaceNaming()
    coord_keys: tuple[str, ...] = KERNEL_COORD_KEYS

    def is_empty(self) -> bool:
        return not self.classes

    def class_of(self, name: str) -> MemoryClass | None:
        for item in self.classes:
            if item.name == name:
                return item
        return None

    @property
    def fallback_class(self) -> MemoryClass | None:
        for item in self.classes:
            if item.fallback:
                return item
        return None


EMPTY_ROUTE_TABLE = RouteTable()


# ====================================================================== #
# 判定的输入与输出
# ====================================================================== #


@dataclass
class RouteContext:
    """一次判定的输入。

    ``candidates`` 是**已鉴权**的候选空间：由 API 层按归属坐标渲染出相关空间、再与调用方
    有写权的空间取交后给出。判定只在集内选择，因此判定实现无从扩权。``fallback`` 恒在集内
    ——不在集内时 API 层整体拒绝写入，判不准时无处可落。

    经源单元的瞬态 metadata 键 ``route_ctx`` 传入构建层，存储层写入前移除、不落盘。
    """

    coords: dict[str, str] = field(default_factory=dict)
    candidates: tuple[Scope, ...] = ()
    fallback: Scope = field(default_factory=Scope)
    classes: tuple[MemoryClass, ...] = ()
    narrow_dims: tuple[NarrowDim, ...] = ()

    def class_of(self, name: str) -> MemoryClass | None:
        for item in self.classes:
            if item.name == name:
                return item
        return None

    def candidate_of(self, space: str) -> Scope | None:
        for scope in self.candidates:
            if scope.space == space:
                return scope
        return None


@dataclass
class RouteDecision:
    """逐条判定结果。

    ``scope`` 是落点空间（主体维恒为空），``tags`` 是该条要写入的判定标签键值对，
    ``memory_class`` 是命中的类别名——走 fallback 时写 fallback 类别名。

    ``discarded`` 为真表示该条判为丢弃：派生单元直接剔除，而 API 层的单条判定入口不剔除
    ——落盘的是调用方给的内容，改落 fallback。
    """

    unit: MemoryUnit | None = None
    scope: Scope = field(default_factory=Scope)
    tags: dict[str, str] = field(default_factory=dict)
    memory_class: str = ""
    discarded: bool = False
    reason: str = ""


# ====================================================================== #
# 契约
# ====================================================================== #


class RouterProducer(Factory):
    """Router 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``router_impl`` 下以 ``@RouterProducer.register("<名>")``
    自注册——注册发生在 import 实现模块时，由
    :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "router"


class Router(ConstructionOperator):
    """归属判定算子。"""

    @abstractmethod
    def route(self, units: list[MemoryUnit], ctx: RouteContext) -> list[RouteDecision]:
        """判定一批候选单元的落点与标签，返回与输入等长的结果列表。

        每批一次模型调用，不逐条调用。只在 ``ctx.candidates`` 内选择落点。
        """

    @property
    @abstractmethod
    def table(self) -> RouteTable:
        """本实现装配时解析出的判定表。

        判定表与实现同源、同一次解析产出（见 :func:`parse_route_table`），因此由实现持有
        并向上暴露：另设一条解析路径供 API 层单独读配置，两条路径对同一份配置得出不同产物
        时，写入边界拒绝的键集合与判定实际写入的键集合会不一致。
        """


def optional_router(config: Any) -> Router | None:
    """按配置取归属判定算子；未声明 ``router`` 命名空间即返回 ``None``。

    与 ``classifier`` / ``layer_annotator`` 的可选装配同形。三个消费方（两个 Evolver 实现
    与 API 层）共用本函数，各写一份的后果是「装配了但某一路取不到」——表现为同一部署里
    构建层判定生效、API 层的单条判定入口不生效。
    """
    if RouterProducer.TOP_NAME in getattr(config, "params", {}):
        return RouterProducer.dep(config)
    namespaces = getattr(config.ctx, "namespaces", {})
    if "default" not in namespaces.get(RouterProducer.TOP_NAME, {}):
        return None
    return RouterProducer.build_named("default", config.ctx)


# ====================================================================== #
# 判定表解析与加载期校验
# ====================================================================== #


def _render_template(template: str, coords: Mapping[str, str]) -> str:
    if not template:
        return ""
    keys = _PLACEHOLDER.findall(template)
    values: dict[str, str] = {}
    for key in keys:
        value = str(coords.get(key, "") or "").strip()
        if not value:
            return ""
        values[key] = value
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _as_str(raw: Any) -> str:
    return "" if raw is None else str(raw).strip()


def _as_str_tuple(raw: Any, *, name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValidationError(f"router: {name} 须是字符串列表，得到 {type(raw).__name__}")
    return tuple(_as_str(item) for item in raw if _as_str(item))


def _parse_classes(raw: Any) -> tuple[MemoryClass, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("router: memory_classes 须是列表")
    classes: list[MemoryClass] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValidationError("router: memory_classes 的每一项须是映射")
        name = _as_str(item.get("name"))
        if not name:
            raise ValidationError("router: memory_classes 的每一项须声明 name")
        if name in seen:
            raise ValidationError(f"router: memory_classes 类别名重复：{name!r}")
        seen.add(name)
        classes.append(
            MemoryClass(
                name=name,
                description=_as_str(item.get("description")),
                owner=_as_str(item.get("owner")),
                space_template=_as_str(item.get("space_template")),
                cross_user=_as_bool(item.get("cross_user")),
                record_only=_as_bool(item.get("record_only")),
                members=_as_str(item.get("members")),
                sanitized=_as_bool(item.get("sanitized")),
                fallback=_as_bool(item.get("fallback")),
                tag_key=_as_str(item.get("tag_key")),
            )
        )
    return tuple(classes)


def _parse_narrow_dims(raw: Any) -> tuple[NarrowDim, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("router: narrow_dims 须是列表")
    dims: list[NarrowDim] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValidationError("router: narrow_dims 的每一项须是映射")
        entity = _as_str(item.get("entity"))
        tag_key = _as_str(item.get("tag_key"))
        if not entity or not tag_key:
            raise ValidationError("router: narrow_dims 的每一项须声明 entity 与 tag_key")
        if tag_key in seen:
            raise ValidationError(f"router: narrow_dims 标签键重复：{tag_key!r}")
        seen.add(tag_key)
        dims.append(
            NarrowDim(
                entity=entity,
                tag_key=tag_key,
                applies_to=_as_str_tuple(item.get("applies_to"), name="applies_to"),
                question=_as_str(item.get("question")),
            )
        )
    return tuple(dims)


def record_tag_keys(classes: Iterable[MemoryClass]) -> tuple[str, ...]:
    """记录维生成的键：``record_only`` 类别各出一个标签键（未声明时按 ``<owner>_id`` 生成）。"""
    keys: list[str] = []
    for item in classes:
        if not item.record_only:
            continue
        key = item.tag_key or (f"{item.owner}_id" if item.owner else "")
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def tag_keys_of(
    classes: Iterable[MemoryClass], narrow_dims: Iterable[NarrowDim]
) -> frozenset[str]:
    """判定标签键集合 = 收窄维标签键 ∪ 记录维生成的键。"""
    return frozenset({dim.tag_key for dim in narrow_dims} | set(record_tag_keys(classes)))


def parse_route_table(raw: Mapping[str, Any] | None) -> RouteTable:
    """解析判定表配置并执行加载期十三条校验，任一不过即抛 :class:`ValidationError`。

    配置为空即返回 :data:`EMPTY_ROUTE_TABLE`（未启用该特性）。
    """
    if not raw:
        return EMPTY_ROUTE_TABLE

    declared = _as_str_tuple(raw.get("coord_entities"), name="coord_entities")
    # 第 12 条在求并之前校验：求并之后交集恒为空，判不出来。
    kernel_clash = sorted(set(declared) & set(KERNEL_COORD_KEYS))
    if kernel_clash:
        raise ValidationError(
            f"router: coord_entities 不得声明内核自带坐标 {kernel_clash}"
            "——接入方取值会覆盖内核由身份推导的权威取值"
        )
    coord_keys = tuple(KERNEL_COORD_KEYS) + tuple(
        key for key in declared if key not in KERNEL_COORD_KEYS
    )

    classes = _parse_classes(raw.get("memory_classes"))
    narrow_dims = _parse_narrow_dims(raw.get("narrow_dims"))
    if not classes:
        raise ValidationError(
            "router: memory_classes 不得为空——声明了该命名空间即须给出类别"
        )

    class_names = {item.name for item in classes}
    coord_key_set = set(coord_keys)
    record_keys = set(record_tag_keys(classes))

    for item in classes:
        # 1 owner=user 的类别不得 cross_user
        if item.owner == "user" and item.cross_user:
            raise ValidationError(
                f"router: 类别 {item.name!r} 的归属维是 user，不得声明 cross_user"
                "——用户个人事实跨 user 可见即泄露"
            )
        # 2 cross_user 类别必须有成员来源或已声明脱敏
        if item.cross_user and not item.members and not item.sanitized:
            raise ValidationError(
                f"router: 类别 {item.name!r} 声明了 cross_user，须给出 members 或 sanitized"
                "——两者皆无即对全组织敞开"
            )
        if item.record_only:
            # 13 record_only 类别须能生成标签键：tag_key 显式声明，或由 owner 按
            # <owner>_id 生成。两者皆无时 record_tag_key_of 返回空串，该类别不产生任何
            # 标签键——命中它的条目只落 fallback 空间、归属实体一点不记，声明整体失效
            # 且不报错。
            if not item.tag_key and not item.owner:
                raise ValidationError(
                    f"router: record_only 类别 {item.name!r} 须声明 tag_key 或 owner"
                    "——两者皆无即生成不出标签键，该类别的声明整体失效"
                )
            continue
        # 9 空间名模板只引用坐标键集合内的键
        placeholders = set(_PLACEHOLDER.findall(item.space_template))
        unknown = sorted(placeholders - coord_key_set)
        if unknown:
            raise ValidationError(
                f"router: 类别 {item.name!r} 的 space_template 引用了未声明的坐标键 {unknown}"
            )
        # 10 空间名模板必须引用该类别的 owner
        if not item.owner or item.owner not in placeholders:
            raise ValidationError(
                f"router: 类别 {item.name!r} 的 space_template 必须引用其 owner "
                f"{item.owner!r}——否则同一归属维的多个实体落进同一个空间"
            )

    # 11 空间名模板不得重复
    templates: dict[str, str] = {}
    for item in classes:
        if item.record_only:
            continue
        clash = next(
            (name for name, tpl in templates.items() if tpl == item.space_template), ""
        )
        if clash:
            raise ValidationError(
                f"router: 类别 {item.name!r} 与 {clash!r} 的 space_template 相同"
                "——两个类别落同一空间"
            )
        templates[item.name] = item.space_template

    # 4 fallback 类别有且仅有一个；5 fallback 类别须 cross_user=false
    fallbacks = [item for item in classes if item.fallback]
    if len(fallbacks) != 1:
        raise ValidationError(
            f"router: fallback 类别须有且仅有一个，当前 {len(fallbacks)} 个——兜底落点不确定"
        )
    fallback = fallbacks[0]
    if fallback.cross_user:
        raise ValidationError(
            f"router: fallback 类别 {fallback.name!r} 不得 cross_user——兜底落点不是最窄空间"
        )
    if fallback.record_only:
        raise ValidationError(
            f"router: fallback 类别 {fallback.name!r} 不得 record_only——它须有自己的空间"
        )

    for dim in narrow_dims:
        # 3 收窄维的 entity 须在坐标键集合内可解析
        if dim.entity not in coord_key_set:
            raise ValidationError(
                f"router: 收窄维 {dim.tag_key!r} 的 entity {dim.entity!r} 不在坐标键集合内"
                "——谓词构造为空值，检索表现为全部放行且不报错"
            )
        # 6 applies_to 只引用已声明的类别
        unknown_classes = sorted(set(dim.applies_to) - class_names)
        if unknown_classes:
            raise ValidationError(
                f"router: 收窄维 {dim.tag_key!r} 的 applies_to 引用了未声明的类别 "
                f"{unknown_classes}"
            )
        # 7 收窄维标签键不得与记录维生成的键同名
        if dim.tag_key in record_keys:
            raise ValidationError(
                f"router: 收窄维标签键 {dim.tag_key!r} 与记录维生成的键同名"
                "——两套语义写同一个键"
            )

    tag_keys = tag_keys_of(classes, narrow_dims)
    # 8 判定标签键不得与内核系统元数据键同名
    reserved_clash = sorted(tag_keys & set(KERNEL_SYSTEM_METADATA_KEYS))
    if reserved_clash:
        raise ValidationError(
            f"router: 判定标签键与内核系统元数据键同名 {reserved_clash}——判定产物覆盖内核字段"
        )

    naming = SpaceNaming(
        templates=tuple(templates.items()),
        fallback_class=fallback.name,
    )
    return RouteTable(
        classes=classes,
        narrow_dims=narrow_dims,
        tag_keys=tag_keys,
        naming=naming,
        coord_keys=coord_keys,
    )


# ====================================================================== #
# 两个落盘不变量与判定应用
# ====================================================================== #


def with_all_tag_keys(tags: Mapping[str, str] | None, ctx: RouteContext) -> dict[str, str]:
    """补齐本次未出现的全部判定标签键为空串（判定产物场景，取值一律折成字符串）。

    两族谓词都依赖「键恒存在」：集合谓词 ``IN ("", value)`` 在字段缺失时判为不匹配，
    靠「不写键表示默认值」会静默收窄——条目查不到且不报错。因此全部收窄维标签键恒写入，
    包括该条目所属类别不适用的维与判为否的维，取值为空串。

    该不变量的定义域是「落盘条目」而不是「判定产物」，因此不经判定的写入路径同样要补，
    见 :func:`fill_missing_tag_keys`。
    """
    filled = {key: "" for key in tag_keys_of(ctx.classes, ctx.narrow_dims)}
    for key, value in (tags or {}).items():
        filled[key] = "" if value is None else str(value)
    return filled


def fill_missing_tag_keys(
    metadata: Mapping[str, Any] | None, tag_keys: frozenset[str]
) -> dict[str, Any] | None:
    """补齐条目 metadata 里缺失的判定标签键为空串，已有键保持原值原类型。

    与 :func:`with_all_tag_keys` 同一个不变量的两种场景：那一支的入参是判定产物、取值全
    是判定结果，这一支的入参是完整的条目 metadata、含调用方写入的任意标量，因此不折算
    已有取值。

    使用点是不经判定的写入路径（调用方显式传 ``scope``）与存量回填。缺这一处时同一空间内
    两条写入路径产出的条目在带 ``coords`` 的检索里表现不同：判定路径写的能召回，显式
    ``scope`` 写的被静默漏掉。
    """
    if not tag_keys:
        return dict(metadata) if metadata is not None else None
    missing = {key: "" for key in tag_keys if key not in (metadata or {})}
    if not missing:
        return dict(metadata) if metadata is not None else None
    return {**(metadata or {}), **missing}


def _fallback_class_name(ctx: RouteContext, default: str = "") -> str:
    return next((item.name for item in ctx.classes if item.fallback), default)


def space_for_class(item: MemoryClass, coords: Mapping[str, str]) -> str:
    """渲染该类别在本次坐标下的空间名；``record_only`` 类别与坐标缺项时返回空串。"""
    if item.record_only:
        return ""
    return _render_template(item.space_template, coords)


def record_tag_key_of(item: MemoryClass) -> str:
    """``record_only`` 类别生成的记录维标签键；非记录维类别返回空串。"""
    if not item.record_only:
        return ""
    return item.tag_key or (f"{item.owner}_id" if item.owner else "")


def build_decision(
    unit: MemoryUnit,
    memory_class: str,
    narrow_hits: Iterable[str],
    ctx: RouteContext,
    *,
    discarded: bool = False,
    reason: str = "",
) -> RouteDecision:
    """由「命中类别 + 判为真的收窄维」装配一条判定结果，供各 ``Router`` 实现复用。

    实现只需回答两个问题——这条归哪一类、哪些收窄维为真；落点解析、记录维标签与 fallback
    回落都在此完成。三处回落到 fallback 空间：类别未声明、类别的空间名渲染不出、渲染出的
    空间不在候选集内。前两处在此判，第三处由 :func:`route_batch` 复判一次——那一处是对
    实现自行给出落点的兜底。

    ``record_only`` 类别不落独立空间，落点取 fallback、类别名仍记该类别：``memory_class``
    记的是命中的类别，落点追溯要的正是「判成了 team 记忆，但按设计落回主空间」这一对照。
    """
    item = ctx.class_of(memory_class)
    if item is None:
        return RouteDecision(
            unit=unit,
            scope=ctx.fallback,
            memory_class=_fallback_class_name(ctx),
            discarded=discarded,
            reason=reason or f"unknown memory class {memory_class!r}",
        )

    resolved_class = item.name
    target = ctx.fallback
    if not item.record_only:
        space = space_for_class(item, ctx.coords)
        candidate = ctx.candidate_of(space) if space else None
        if candidate is not None:
            target = candidate
        else:
            resolved_class = _fallback_class_name(ctx, resolved_class)
            reason = reason or "class space not in the authorized candidate set"

    tags: dict[str, str] = {}
    for dim in ctx.narrow_dims:
        if not dim.applies(resolved_class) or dim.tag_key not in set(narrow_hits):
            continue
        value = str(ctx.coords.get(dim.entity, "") or "").strip()
        if value:
            tags[dim.tag_key] = value
    record_key = record_tag_key_of(item)
    if record_key:
        value = str(ctx.coords.get(item.owner, "") or "").strip()
        if value:
            tags[record_key] = value

    return RouteDecision(
        unit=unit,
        scope=target,
        tags=tags,
        memory_class=resolved_class,
        discarded=discarded,
        reason=reason,
    )


def enforce_sanitized(decision: RouteDecision, ctx: RouteContext) -> RouteDecision:
    """目标类别声明「不含主体标识」时做一次确定性检查，命中即改落 fallback。

    不改写内容，也不阻断整批：脱敏声明约束的是落点，改写内容会让落盘产物与调用方给的
    内容不一致，而阻断会让一条判错的记忆拖垮整次写入。

    检查项取归属坐标里的内核三项取值（它们以身份为准、不接受调用方覆盖），逐项在内容中
    作子串查找。判定实现自身也可能做同类检查，此处再做一次是因为换一个实现即可能漏掉。
    """
    target = ctx.class_of(decision.memory_class)
    if target is None or not target.sanitized or decision.discarded:
        return decision
    unit = decision.unit
    content = unit.content if unit is not None else ""
    if not content:
        return decision
    for key in KERNEL_COORD_KEYS:
        value = str(ctx.coords.get(key, "") or "").strip()
        if value and value in content:
            logger.warning(
                "Router.enforce_sanitized: class=%s 命中主体标识 coord=%s，改落 fallback",
                decision.memory_class,
                key,
            )
            return replace(
                decision,
                scope=ctx.fallback,
                memory_class=_fallback_class_name(ctx, decision.memory_class),
                reason="sanitized check hit a principal identifier",
            )
    return decision


def _fallback_decision(unit: MemoryUnit, ctx: RouteContext, reason: str) -> RouteDecision:
    return RouteDecision(
        unit=unit,
        scope=ctx.fallback,
        memory_class=_fallback_class_name(ctx),
        reason=reason,
    )


def route_batch(
    router: Router | None, units: Sequence[MemoryUnit], ctx: RouteContext
) -> list[RouteDecision]:
    """判定一批单元并套用两个落盘不变量，返回与输入等长的结果。

    三种情形一律落 fallback，不阻断写入：未装配、判定抛异常、判定给出的落点不在候选集内。
    最后一种是落点约束的兜底——「判定不得扩权」这条不靠实现自觉。
    """
    if not units:
        return []
    decisions: list[RouteDecision] | None = None
    if router is not None:
        try:
            raw = router.route(list(units), ctx)
            if len(raw) == len(units):
                decisions = list(raw)
            else:
                logger.warning(
                    "Router.route 返回 %d 条、输入 %d 条，全批落 fallback",
                    len(raw),
                    len(units),
                )
        except Exception as exc:  # noqa: BLE001 —— 判定失败不阻断写入
            logger.warning("Router.route 失败，全批落 fallback：%s", exc)
    if decisions is None:
        decisions = [_fallback_decision(unit, ctx, "router unavailable") for unit in units]

    applied: list[RouteDecision] = []
    for unit, decision in zip(units, decisions):
        if decision is None:
            decision = _fallback_decision(unit, ctx, "router returned no decision")
        decision = replace(decision, unit=unit)
        if not decision.discarded and ctx.candidate_of(decision.scope.space) is None:
            logger.warning(
                "Router.route: 落点 %r 不在候选集内，改落 fallback", decision.scope.space
            )
            decision = replace(
                decision,
                scope=ctx.fallback,
                memory_class=_fallback_class_name(ctx, decision.memory_class),
                reason="target space outside the authorized candidate set",
            )
        decision = enforce_sanitized(decision, ctx)
        decision = replace(decision, tags=with_all_tag_keys(decision.tags, ctx))
        applied.append(decision)
    return applied


def apply_decisions(decisions: Sequence[RouteDecision]) -> list[MemoryUnit]:
    """把判定结果写回单元：改 scope、写判定标签与类别记录键，剔除判为丢弃的。

    ``memory_class`` 是落点断言的唯一稳定判据——派生记忆的 ``content`` 是模型产物、措辞不
    保证逐次一致，按内容等值匹配不成立。
    """
    kept: list[MemoryUnit] = []
    for decision in decisions:
        unit = decision.unit
        if unit is None or decision.discarded:
            continue
        unit.scope = decision.scope
        metadata = dict(unit.system_metadata or {})
        # 判定上下文用完即弃：派生单元的 metadata 从源单元复制而来，其中带着这个瞬态键。
        # 存储层写入前会剥除它，但落盘产物要回传给调用方——不在此剥除即把一个内部对象
        # 交到调用方手里，且它与落盘条目的 metadata 不一致。
        metadata.pop(ROUTE_CTX_KEY, None)
        metadata.update(decision.tags)
        if decision.memory_class:
            metadata[MEMORY_CLASS_KEY] = decision.memory_class
        unit.system_metadata = metadata
        kept.append(unit)
    return kept
