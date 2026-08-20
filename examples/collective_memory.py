"""群体记忆端到端演示：写入的三条路径、跨空间检索、退出即失效与删除连带。

运行（离线，判定取脚本内的关键词桩）：

    PYTHONPATH=. python examples/collective_memory.py

运行（在线，判定取真实模型）：在项目根 ``.env`` 或环境变量里配齐三项后同样命令运行——

    OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL

三项齐备时脚本把 ``router.default`` 的 target 由 ``keyword_demo`` 换成 ``llm``，并就地内联
一个 OpenAI 兼容客户端作它的 ``llm`` 依赖；缺任一项即回落离线桩。内联而非引用共享的
``llm.default``，是为了让本例只把归属判定接到线上，抽取、分类等其余算子仍走离线默认实现，
线上线下的差异被限制在「这一条归哪一类」这一处。

可选 ``LLM_TARGET`` 指定客户端实现名（缺省 ``openai``）。默认开思考模式的模型须取
``dashscope``——它在请求体里带 ``enable_thinking=false``；不关思考时这类模型把回复放进
``reasoning_content``、``content`` 返空串，判定解析失败后整批回落 fallback。

一次连贯的协作流程，按八个步骤覆盖 S09 的八项能力：

| 步骤 | 覆盖 |
|---|---|
| 1 开通 | 主空间保持个体形态、协作空间逐参与者写成员记录（五类空间开通契约里的两类）|
| 2 分流 | 省略 scope，一次会话的三句话按归属坐标落进三个不同去处 |
| 3 直写 | 传了 scope 即不判定：落盘 scope 归一为两维、标签键补齐空串，两条写入边界拒绝 |
| 4 批量 | 一批多条只判定一次，落点仍逐条各算 |
| 5 隔离 | 协作空间成员互见；个人主空间他人不可达 |
| 6 收窄 | 第二族谓词按归属坐标收窄，标签为空的条目一并命中 |
| 7 退出 | 移除成员记录后立刻失效 |
| 8 清理 | 项目删除时，接入方按谓词清各主空间里带该项目标签的条目，再删协作空间 |

结论怎么产生：本例不预先写死每一步的结论，每处结论都取自当次运行读回的落盘事实。
核对分两类：

- 不变量——与判定实现无关，任何一次运行都须成立。落盘 scope 归一、标签键补齐、两条写入
  边界拒绝、越权拒绝、等值谓词的删除范围属此类。不成立即打「不符」，计入收尾汇总，
  进程以非零码退出。
- 判定相关的预期——取决于「这一条归哪一类、哪些收窄维为真」，线上判定与离线桩可以不同。
  不成立即打「存疑」并附退化说明，不计入失败；这正是要观察的对象。

第 6、8 两幕的观察点依赖「个人偏好是否被判为与 apollo 相关」这一判定结果。该结果为否时两步
退化为空操作，解说随之改写，不再宣称收窄或清除发生过。两种判定实现的接线、落点约束与落盘
不变量完全相同，差别只在「这一条归哪一类」由谁回答。
"""

from __future__ import annotations

import logging
import os
import sys

import yaml

from jiuwen_memory.api import assemble
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole
from jiuwen_memory.common.type_def import Context, FilterClause, FilterOp, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.router import (
    Router,
    RouterProducer,
    RouteTable,
    build_decision,
    parse_route_table,
)
from jiuwen_memory.control import BatchWriteItem, SpaceMember, SpaceSpec
from jiuwen_memory.control.types import DeleteMode, DeleteSelector

logger = logging.getLogger(__name__)

ORG = "acme"
# 运维通道：组织级入口（建空间）由角色闸门裁决，过渡期无组织级角色，取空身份。
OPS = Scope()
# 数据面身份：经代理发起，session 维参与收窄、不参与落点。
ALICE = Scope(org=ORG, user="alice", agent="assistant", session="s1")
BOB = Scope(org=ORG, user="bob", agent="assistant", session="s2")
# 治理身份：本人直接调用，不带代理维。
#
# 归属主体档分两级：治理动作与整空间导出要求三个主体维逐维相同（本人直接调用），
# 条目读写只要求登记项的非空主体维在调用方身份上取值相同（经名下代理调用一并覆盖）。
# 用带代理维的身份调 add_space_member 会得到 PermissionDeniedError——这是有意的：
# 代理不得代替用户处置空间。
ALICE_PERSON = Scope(org=ORG, user="alice")

ALICE_SPACE = "u_alice"
BOB_SPACE = "u_bob"
PROJECT_SPACE = "p_apollo"

# 步骤 2写入的三句话，后续各步按内容回读它们的落盘事实。
PREFERENCE = "我习惯用简洁的风格回复"
PROJECT_FACT = "项目 apollo 的部署环境是集群 A"
TEAM_RULE = "团队评审必须两人以上通过"
# 步骤 3的直写内容：不经判定，判定标签键全部补空串。
DIRECT_WRITE = "登录超时统一设成 30 分钟"
# 步骤 4的批量内容：一条个人、一条项目，预期落点不同。
BATCH_PREFERENCE = "我不喜欢在回复里加表情"
BATCH_PROJECT = "项目 apollo 的灰度比例先设百分之五"


# ====================================================================== #
# 演示用判定实现：按关键词作答，使本例可离线运行
# ====================================================================== #


class KeywordDemoRouter(Router):
    """按关键词判类别的演示桩。

    生产用 ``llm``：一批候选发一次模型调用，逐条产出「命中哪个类别」与「哪些收窄维为真」。
    落点解析、记录维标签、fallback 回落与两个落盘不变量都不在实现内部——它们由
    ``construction.router`` 的公共函数承担，换一个实现不会漏掉。
    """

    def __init__(self, table: RouteTable) -> None:
        self._table = table

    @property
    def table(self) -> RouteTable:
        return self._table

    def operator_type(self) -> OperatorType:
        return OperatorType.ROUTER

    def health(self) -> None:
        return None

    def route(self, units, ctx):
        decisions = []
        for unit in units:
            content = unit.content
            if "项目" in content or "apollo" in content.lower():
                memory_class, hits = "project_memory", ("project_id",)
            elif "团队" in content or "team" in content.lower():
                memory_class, hits = "team_memory", ("team_id",)
            else:
                # 个人偏好：判为与当前项目相关，因此打上项目标签——它落个人主空间，
                # 但检索时能按项目收窄出来。
                memory_class, hits = "user_memory", ("project_id",)
            decisions.append(build_decision(unit, memory_class, hits, ctx))
        return decisions


@RouterProducer.register("keyword_demo")
def _build_keyword_demo(config):
    return KeywordDemoRouter(
        parse_route_table(
            {
                "coord_entities": config.get("coord_entities"),
                "memory_classes": config.get("memory_classes"),
                "narrow_dims": config.get("narrow_dims"),
            }
        )
    )


# ====================================================================== #
# 装配配置：按环境变量在离线桩与线上模型之间切换
# ====================================================================== #

# 与 tests/unit/control/test_middle_e2e_real_llm.py 同一组变量名，三项须齐备。
_ONLINE_ENV = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")

# 可选：LlmProducer 的实现名。取 dashscope 时请求体带 enable_thinking=false——
# 默认开思考的模型（GLM、Qwen3 等）不关思考会把 content 返成空串，判定随即整批回落。
_LLM_TARGET_ENV = "LLM_TARGET"

# 判定表的三个配置键，两种判定实现读的是同一份。
_TABLE_KEYS = ("coord_entities", "memory_classes", "narrow_dims")


def _load_env_file() -> None:
    """加载项目根 ``.env``（该文件已在 .gitignore 内）；未安装 python-dotenv 时跳过。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _load_config(cfg_path: str) -> tuple[Config, str, RouteTable]:
    """读装配配置，并按环境变量决定归属判定取哪一种实现。

    返回 ``(配置, 判定实现说明, 判定表)``。三项环境变量齐备时把 ``router.default`` 的
    target 由 ``keyword_demo`` 换成 ``llm``，并就地内联一个 OpenAI 兼容客户端作它的 ``llm``
    依赖；判定表三项不动——两种实现读的是同一份判定表。

    判定表在此就地解析一份返回：本例后续要用它的标签键集合核对落盘不变量，而 ``MemoryAPI``
    没有公开的判定表入口，示例不去读实现的内部属性。

    内联而非引用共享的 ``llm.default``：本例只把归属判定接到线上，其余算子仍用离线默认
    实现，线上线下的差异被限制在判定这一处。
    """
    _load_env_file()
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    router = data["router"]["default"]
    params = router.setdefault("params", {})
    table = parse_route_table({key: params.get(key) for key in _TABLE_KEYS})

    creds = {name: os.environ.get(name, "").strip() for name in _ONLINE_ENV}
    missing = [name for name, value in creds.items() if not value]
    if missing:
        return Config.from_dict(data), f"离线关键词桩；未配置 {'/'.join(missing)}", table

    target = os.environ.get(_LLM_TARGET_ENV, "").strip() or "openai"
    llm_params = {
        "llm_api_key": creds["OPENAI_API_KEY"],
        "llm_base_url": creds["OPENAI_BASE_URL"],
        "llm_model": creds["OPENAI_MODEL"],
        "llm_temperature": 0.0,
        "llm_max_tokens": 4096,
    }
    if target == "dashscope":
        llm_params["enable_thinking"] = False

    router["target"] = "llm"
    params["llm"] = {"target": target, "params": llm_params}
    # 判定失败不阻断写入：重试用尽后整批回落 fallback 空间，见 construction.router。
    params.setdefault("retry_max_retries", 3)
    return Config.from_dict(data), f"线上模型 {creds['OPENAI_MODEL']}（{target}）", table


# ====================================================================== #
# 事实核对：结论取自当次运行读回的落盘事实，不预先写死
# ====================================================================== #

_FAILURES: list[str] = []


def _invariant(label: str, ok: bool, detail: str = "") -> bool:
    """不变量：与判定实现无关，任何一次运行都须成立；不成立即计入收尾汇总。"""
    logger.info("  [%s] %s%s", "符合" if ok else "不符", label, f"　{detail}" if detail else "")
    if not ok:
        _FAILURES.append(label)
    return ok


def _expect(label: str, ok: bool, detail: str = "", degraded: str = "") -> bool:
    """判定相关的预期：线上判定可与离线桩不同，不符只作说明，不计入失败。"""
    logger.info("  [%s] %s%s", "符合" if ok else "存疑", label, f"　{detail}" if detail else "")
    if not ok and degraded:
        for line in degraded.splitlines():
            logger.info("       %s", line)
    return ok


def _report() -> int:
    if not _FAILURES:
        logger.info("\n[核对] 全部不变量成立")
        return 0
    logger.info("\n[核对] %d 项不变量不成立：", len(_FAILURES))
    for item in _FAILURES:
        logger.info("    - %s", item)
    return 1


# ====================================================================== #
# 辅助输出与事实回读
# ====================================================================== #


_RULE = "─" * 66


def _act(title: str) -> None:
    """步骤标题：空行加一条定长横线，把八个步骤在输出里分开。"""
    logger.info("")
    logger.info(_RULE)
    logger.info("%s", title)


def _units(api, space: str, identity: Scope) -> list:
    return api.list(Scope(org=ORG, space=space), identity=identity, limit=50).items


def _find(api, space: str, identity: Scope, content: str):
    """按内容回读一条落盘单元；返回 None 表示这一条本次没落在这个空间。"""
    for unit in _units(api, space, identity):
        if unit.content == content:
            return unit
    return None


def _tag(unit, key: str) -> str:
    """读一个判定标签的取值；单元不存在或键缺失都返回空串，键缺失另由不变量核对。"""
    if unit is None:
        return ""
    value = unit.system_metadata.get(key)
    return "" if value is None else str(value)


def _dump_space(api, space: str, identity: Scope, label: str = "") -> None:
    """列一个空间的落盘条目：内容、类别、非空的判定标签。

    只打非空标签：标签键恒存在（判为否的写空串），逐行打全会让每条多出两三个恒为空的位。
    键是否齐全由步骤 2、3 的不变量核对，不靠这里逐行展示；作者主体标记本例不比对。
    """
    items = _units(api, space, identity)
    logger.info("  [%s] %s 条%s", space, len(items), f"  ({label})" if label else "")
    for unit in items:
        meta = unit.system_metadata
        tags = " ".join(
            f"{key}={meta[key]}"
            for key in ("project_id", "team_id", "session_id")
            if meta.get(key)
        )
        row = f"{unit.content[:24]:<26}{meta.get('memory_class') or '(直写)':<16}{tags}"
        logger.info("      %s", row.rstrip())


# ====================================================================== #
# 各步骤
# ====================================================================== #


def _act_provision(api) -> None:
    """步骤 1。

    主空间必须保持成员表为空：代理收窄与作者隔离都依赖这一点。
    加成员用不带代理维的身份——治理动作要求本人直接调用（归属主体档第一级）。
    """
    _act("步骤 1　开通：主空间保持个体形态，协作空间逐参与者写成员记录")
    api.create_space(
        SpaceSpec(org=ORG, space=ALICE_SPACE, owner=Scope(org=ORG, user="alice")), identity=OPS
    )
    api.create_space(
        SpaceSpec(org=ORG, space=BOB_SPACE, owner=Scope(org=ORG, user="bob")), identity=OPS
    )
    logger.info("  预建主空间 %s / %s，各登记一个归属主体、不写成员记录", ALICE_SPACE, BOB_SPACE)

    # 协作空间：由项目负责人建，逐参与者写一条成员记录。成员表非空即共享形态成立。
    api.create_space(
        SpaceSpec(org=ORG, space=PROJECT_SPACE, owner=Scope(org=ORG, user="alice")), identity=OPS
    )
    api.add_space_member(
        ORG,
        PROJECT_SPACE,
        SpaceMember(
            scope=Scope(org=ORG, user="bob"),
            content_role=SpaceContentRole.EDITOR,
            governance_role=SpaceGovernanceRole.NONE,
        ),
        identity=ALICE_PERSON,
    )
    logger.info(
        "  建协作空间 %s，写入 bob 的成员记录（内容轴 editor、治理轴 none）", PROJECT_SPACE
    )


def _act_route(api, table: RouteTable, coords: dict[str, str]) -> None:
    """步骤 2。判定结果不外传：第 6、8 幕各自从落盘条目回读自己要的那部分事实。"""
    _act("步骤 2　分流：一次会话的三句话，省略 scope，按归属坐标各落各处")
    logger.info("  coords=%s（user/agent/session 三项以身份为准，不接受调用方覆盖）", coords)
    logger.info("  输入 3 条，均省略 scope：")
    for sentence in (PREFERENCE, PROJECT_FACT, TEAM_RULE):
        landed = api.add(sentence, identity=ALICE, coords=coords)[0]
        logger.info(
            "    「%s」→ %s  %s",
            sentence,
            landed.scope.space,
            landed.system_metadata.get("memory_class"),
        )
    logger.info("\n  落盘结果：")
    _dump_space(api, ALICE_SPACE, ALICE)
    _dump_space(api, PROJECT_SPACE, ALICE)

    # 回读三条的落盘事实，后续各步的解说全部以此为准。
    preference = _find(api, ALICE_SPACE, ALICE, PREFERENCE)
    team_rule = _find(api, ALICE_SPACE, ALICE, TEAM_RULE)
    logger.info("")
    if preference is None:
        _invariant("个人偏好落在调用方主空间", False, "未找到该条目")
    else:
        missing = sorted(table.tag_keys - set(preference.system_metadata))
        _invariant(
            "落盘条目写齐了全部判定标签键（判为否的写空串）",
            not missing,
            f"缺键={missing or '无'}",
        )

    # 第 6、8 两幕的观察点系于这一个判定结果。
    preference_project = _tag(preference, "project_id")
    _expect(
        "个人偏好被判为与 apollo 相关，因而带上 project_id=apollo",
        preference_project == "apollo",
        f"实际 project_id={preference_project or '空串'}",
        degraded=(
            "本次判定把这条判为与具体项目无关，标签取空串。\n"
            "步骤 6 的收窄对比与步骤 8 的连带清除因此没有观察对象，两步退化为空操作。"
        ),
    )
    _expect(
        "团队约定命中 record_only 类别，落主空间并把 team 记成标签",
        _tag(team_rule, "team_id") == "core",
        f"实际 team_id={_tag(team_rule, 'team_id') or '空串'}",
    )


def _act_direct_write(api, table: RouteTable, coords: dict[str, str]) -> None:
    """步骤 3。三处解说，输出里只留结论：

    - 落盘 scope 归一为两维：主体维留在落盘 scope 上时，判定第 8 步按各维相等放行，
      作者对自己写的条目取得全部动作权、不经内容轴矩阵。
    - 标签键补齐的定义域是落盘条目而非判定产物：不补则同一空间内两条写入路径产出的条目
      在带 coords 的检索里表现不同，且调用方收不到任何提示。不经判定即不写 memory_class。
    - 两条写入边界：入参 scope 的主体维由调用方声明即成为第二条归属判据，与内核按身份
      写入的作者标记指向不同主体；判定标签键参与检索过滤，能自行赋值即可绕过收窄——把一
      条内容的会话标签写成别人的会话 id，即让它出现在别人的上下文里。
    """
    _act("步骤 3　直写：传了 scope 即不判定，只做落盘 scope 归一与标签键补齐")
    direct = api.add(
        DIRECT_WRITE,
        Scope(org=ORG, space=ALICE_SPACE, user="alice", session="s1"),
        identity=ALICE,
    )[0]
    logger.info("  入参 scope=Scope(org, space=%s, user=alice, session=s1)", ALICE_SPACE)
    logger.info("  落盘 scope=%s", direct.scope)
    _invariant(
        "落盘 scope 归一为 org+space 两维，主体维与会话维被去掉",
        not (direct.scope.user or direct.scope.agent or direct.scope.session),
    )

    missing = sorted(table.tag_keys - set(direct.system_metadata))
    nonempty = sorted(key for key in table.tag_keys if direct.system_metadata.get(key))
    _invariant(
        "这条路径同样补齐判定标签键，取值一律空串",
        not missing and not nonempty,
        f"缺键={missing or '无'}　非空键={nonempty or '无'}",
    )

    logger.info("\n  两条写入边界：")
    label = "入参 scope 的主体维与调用方身份不一致即拒绝"
    try:
        api.add("越权写", Scope(org=ORG, space=ALICE_SPACE, user="bob"), identity=ALICE)
        _invariant(label, False, "未拒绝")
    except ValidationError as exc:
        _invariant(label, True, f"{type(exc).__name__}: {exc}")

    label = "调用方在 metadata 里占用判定标签键即拒绝"
    try:
        api.add("占键写", identity=ALICE, coords=coords, system_metadata={"session_id": "s9"})
        _invariant(label, False, "未拒绝")
    except ValidationError as exc:
        _invariant(label, True, f"{type(exc).__name__}: {exc}")


def _act_batch(api, coords: dict[str, str]) -> None:
    """步骤 4。

    判定上下文每批只算一次：coords 与身份批内恒定，候选空间集合与逐空间判权因而是同一份；
    逐项各算一次即把判权次数乘以批大小。
    """
    _act("步骤 4　批量：一批多条只判定一次，落点仍逐条各算")
    batch = api.batch_add(
        [BatchWriteItem(content=BATCH_PREFERENCE), BatchWriteItem(content=BATCH_PROJECT)],
        identity=ALICE,
        coords=coords,
    )
    landed_spaces: list[str] = []
    for outcome in batch.outcomes:
        if outcome.error:
            logger.info("  第 %d 条失败：%s %s", outcome.index, outcome.error_type, outcome.error)
            continue
        unit = outcome.units[0]
        landed_spaces.append(unit.scope.space)
        logger.info(
            "    「%s」→ %s  %s",
            outcome.item.content,
            unit.scope.space,
            unit.system_metadata.get("memory_class"),
        )
    _invariant("批量两条都写入成功", len(landed_spaces) == 2, f"成功 {len(landed_spaces)}/2")
    _expect(
        "一批两条按各自内容分别定落点，未整批取同一结论",
        len(set(landed_spaces)) == 2,
        f"落点={landed_spaces}",
        degraded=(
            "两条落到了同一空间。两种可能，不要只认其中一种：\n"
            "① 判定实现确实把两条判成了同一类别；\n"
            "② 结论对不回条目。LLMRouter 只按 source_id 把模型结论对回输入单元、没有按序\n"
            "   回退，而探针 id 由 collective.route_many 逐条给 uuid；该处一旦退化为空串，\n"
            "   一批多条即互相覆盖、整批取到同一个结论。本例是这条路径的回归防线。\n"
            "区分方法：看上面 LLMRouter 逐条打出的 class 与最终落点是否一致。"
        ),
    )


def _act_isolation(api) -> None:
    """步骤 5。无权空间在判权阶段直接剔除，不报错也不占召回名额。"""
    _act("步骤 5　隔离：协作空间成员互见，个人主空间他人不可达")
    bob_view = api.search_spaces(
        "部署 风格", Context(scope=Scope(org=ORG)), identity=BOB, top_k=10
    )
    bob_contents = [item.content for item in bob_view.items]
    logger.info("  bob 跨空间检索「部署 风格」→ %d 条", len(bob_contents))
    for content in bob_contents:
        logger.info("      %s", content)
    leaked = [
        content for content in bob_contents if _find(api, ALICE_SPACE, ALICE, content) is not None
    ]
    _invariant(
        "alice 主空间的条目一条都没进 bob 的召回结果",
        not leaked,
        f"泄漏={leaked}" if leaked else "",
    )

    label = "bob 直接列举 alice 主空间即拒绝"
    try:
        api.list(Scope(org=ORG, space=ALICE_SPACE), identity=BOB, limit=10)
        _invariant(label, False, "未拒绝")
    except PermissionDeniedError as exc:
        _invariant(label, True, type(exc).__name__)


def _act_narrow(api) -> None:
    """步骤 6。

    留下来的条目各有各的原因：判为否的收窄维写空串；声明了 applies_to 的收窄维对不适用的
    类别也写空串。空串一并命中——「不适用」与「判为否」的条目都不该被收窄掉。
    """
    _act("步骤 6　收窄：第二族谓词按归属坐标筛，标签为空的条目一并命中")
    query = "部署 风格 评审 超时 灰度"
    narrowed: dict[str, set[str]] = {}
    for probe in ({"project": "apollo"}, {"project": "mercury"}):
        result = api.search_spaces(
            query, Context(scope=Scope(org=ORG)), identity=ALICE, coords=probe, top_k=20
        )
        contents = {item.content for item in result.items}
        narrowed[probe["project"]] = contents
        logger.info("  coords=%s → %d 条", probe, len(contents))

    only_apollo = sorted(narrowed["apollo"] - narrowed["mercury"])
    only_mercury = sorted(narrowed["mercury"] - narrowed["apollo"])
    logger.info(
        "  两次探针的差集：apollo 独有=%s　mercury 独有=%s",
        only_apollo or "无",
        only_mercury or "无",
    )
    _invariant("换坐标只会筛掉条目，不会引入新条目", not only_mercury)
    # 被筛掉的应当恰好是「标签非空且不等于探针取值」的那些。带 apollo 标签的条目由本次判定
    # 产生，条数因实现而异；两次探针的差集须与它逐条对上，这一条与判定结果无关，恒须成立。
    tagged = {
        unit.content
        for unit in _units(api, ALICE_SPACE, ALICE)
        if unit.system_metadata.get("project_id") == "apollo"
    }
    _invariant(
        "apollo 独有的条目恰好是带 apollo 标签且本次被召回的那些",
        set(only_apollo) == tagged & narrowed["apollo"],
        f"带标签且被召回={sorted(tagged & narrowed['apollo']) or '无'}",
    )
    if not tagged:
        logger.info("  —— 本次判定没给任何条目打 apollo 标签，收窄对比没有观察对象")


def _act_revoke(api) -> None:
    """步骤 7。判定每次调用都读一次空间事实，降权与移除即时生效，不等缓存过期。"""
    _act("步骤 7　退出：移除成员记录，立刻失效")
    api.remove_space_member(ORG, PROJECT_SPACE, Scope(org=ORG, user="bob"), identity=ALICE_PERSON)
    after = api.search_spaces("部署", Context(scope=Scope(org=ORG)), identity=BOB, top_k=10)
    _invariant(
        "移除成员记录后 bob 再检索取不到协作空间内容",
        not after.items,
        f"仍取到 {len(after.items)} 条",
    )


def _act_cleanup(api) -> None:
    _act("步骤 8　清理：项目删除时，跨空间的项目内容由接入方按谓词清除")
    logger.info("  删除前：")
    _dump_space(api, ALICE_SPACE, ALICE)
    _dump_space(api, PROJECT_SPACE, ALICE)

    before = _units(api, ALICE_SPACE, ALICE)
    tagged = {
        unit.content
        for unit in before
        if unit.system_metadata.get("project_id") == "apollo"
    }
    untagged = {unit.content for unit in before} - tagged

    # 等值谓词天然排除空串，「标签为空的不删」不需要另加条件。
    # 实体删除的连带清理不由内核编排：谁掌握业务实体的关系，谁就知道要清哪些空间。
    # 内核提供的是 DeleteSelector 的结构化谓词——判定标签与作者主体标记都落在条目
    # metadata 上，原有的 tags（标签数组）表达不了。
    removed = api.delete(
        DeleteSelector(
            scope=Scope(org=ORG, space=ALICE_SPACE),
            filters=FilterClause("system_metadata.project_id", FilterOp.EQ, "apollo"),
            mode=DeleteMode.PURGE,
        ),
        identity=ALICE,
    )
    logger.info("\n  第 1 步 delete(%s, project_id=apollo) → 清除 %d 条", ALICE_SPACE, len(removed))
    result = api.delete_space(ORG, PROJECT_SPACE, identity=ALICE_PERSON, mode=DeleteMode.PURGE)
    logger.info("  第 2 步 delete_space(%s) → %s", PROJECT_SPACE, result.deleted_counts)

    remaining = {unit.content for unit in _units(api, ALICE_SPACE, ALICE)}
    logger.info("  删除后 %s 剩 %d 条", ALICE_SPACE, len(remaining))
    _invariant(
        "等值谓词清除的条数等于删除前带 apollo 标签的条数",
        len(removed) == len(tagged),
        f"清除 {len(removed)} 条 / 带标签 {len(tagged)} 条",
    )
    _invariant(
        "标签为空的条目一条不少地保留",
        untagged <= remaining,
        f"丢失={sorted(untagged - remaining) or '无'}",
    )
    if not tagged:
        logger.info("  —— 本次运行没有条目带 apollo 标签，第 1 步是空操作，连带清除未发生")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collective_config.yml")
    config, router_note, table = _load_config(cfg_path)
    api = assemble(config=config)
    # 内核各层的运行日志与本例要讲的事无关，压到 ERROR 只留演示叙述。
    # 须在 assemble 之后设置：装配期的 setup_logging 会按配置重设该 logger 的级别。
    logging.getLogger("agent_memory").setLevel(logging.ERROR)
    # 线上判定的逐条结果由 LLMRouter 以 INFO 打出，压级后会被一并吞掉，单独放行该 logger。
    logging.getLogger("agent_memory.construction.router_impl.llm_router").setLevel(logging.INFO)
    # 线上模式下 httpx / openai SDK 会逐请求打 INFO，与演示叙述无关，压到 WARNING。
    for noisy in ("httpx", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger.info(
        "[装配] 判定实现=%s  类别=%s  判定标签键=%s",
        router_note,
        [item.name for item in table.classes],
        sorted(table.tag_keys),
    )

    coords = {"project": "apollo", "team": "core"}
    _act_provision(api)
    _act_route(api, table, coords)
    _act_direct_write(api, table, coords)
    _act_batch(api, coords)
    _act_isolation(api)
    _act_narrow(api)
    _act_revoke(api)
    _act_cleanup(api)
    return _report()


if __name__ == "__main__":
    sys.exit(main())
