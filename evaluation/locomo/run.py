"""LOCOMO 端到端评测（v5，适配 mem2.0 新装配架构）。

把旧 v5（依赖 jiuwen_memory.*）迁移到当前 api.assemble + LocalMemoryAPI 架构：
- 精简冒烟规模（默认）：每 conv 前 1 session 前 5 轮对话 + 前 2 道 QA（category>4 跳过）
  全量可用环境变量打开（MAX_SESSIONS=0 MAX_TURNS=0 MAX_QA=0 = 全量）
- 单判：generate/judge 统一用 locomo/prompts.py（get_answer_generation_prompt / get_judge_prompt）
- 10 并发：ThreadPoolExecutor（LocalMemoryAPI 同步方法内部 asyncio.run，不能用旧 async gather）
- 只记：各 conv 类别准确率 + 时延 + token 消耗
  不记：提取的记忆 / 错题统计 / 召回的记忆 / 双判 / 分歧题

运行：
    cd d:/project/agent-memory
    PYTHONUTF8=1 python locomo/test_locomo_v5.py
配置：加载 locomo/config.yml（对齐后的真后端：华为云 DeepSeek + bge-m3 + Redis + Milvus +
      Elasticsearch，与 staged eval / smoke_concurrent 同一份配置）。后端需先就绪，
      见 locomo/LOCAL_BACKEND_SETUP.md。config.yml 已含 weighted_rrf 融合 + retriever 阈值置 0。
环境变量（.env 已配华为云 MaaS DeepSeek-V3.2 + BGE-M3）：
    CONCURRENCY=10      外层 conv 并发（10 conv 同时跑，每 conv 内部 cutoff/qa 串行，峰值=此值）
    START_CONV=0        起始 conv
    CONV_IDS=           精确指定 conv 列表（逗号分隔，优先于 START_CONV）
    RECALL_TOP_K=200    召回条数（对齐 mem0 locomo benchmark 召回 200，每路 recall_max=200）
    CUTOFFS=10,20,50,200  多 cutoff 切片评测（对齐 mem0 locomo 默认 10,20,50,200，设单值退化为单点）
    MAX_TURNS=5         每 session 取前 N 轮 turn（0=全量）
    MAX_SESSIONS=0      每 conv 取前 N 个 session（0=全量）
    MAX_QA=2            每 conv 跑 N 道 QA（0=全量）
    RESUME=0            断点恢复（1=保留已完成 conv 跳过）
    RUN_TAG=v1          本轮标签，进产物目录名 + 决定写入 scope 的 org（locomo-infer-<RUN_TAG>）
    OUTPUT_DIR=         完全指定产物目录，不建 runs 子目录（旧行为）
    RAW_FALLBACK=0      抽取零产出时按默认路径补写原文（默认关：测纯抽取质量，见下方说明）
    RAW_ALWAYS=0        每个 turn 都额外写一份原文（与 RAW_FALLBACK 互斥的另一方案，A/B 用）
    DATE_PREFIX=0       正文加绝对日期前缀（默认关：新 prompt 的时间锚点取 observation_date 行）
    DISABLE_THINKING=1  关闭思维链（extra_body 注入），其文本会干扰抽取算子 JSON 解析

产物管理：每轮进 locomo/runs/<时间戳>_<RUN_TAG>/，含 test_*_v5<conv>.* + run_meta.json
          + config.snapshot.yml + engine.log + run.log。locomo/runs/latest.txt 记最近一轮。
          按轮次隔离是必需的：test_response/recall/errors/mem_extract 四类以 "a" 模式追加
          写入，共用目录会跨轮混数据。跑完用 locomo/analyze_run.py 复算中间指标。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv

# ---- 路径与 .env --------------------------------------------------------- #
_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_EVALUATION_DIR = os.path.dirname(_TEST_DIR)
_PROJECT_ROOT = os.path.dirname(_EVALUATION_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)  # 仓库根：jiuwen_memory/ 与 bootstrap/ 均为顶层包
load_dotenv(dotenv_path=os.path.join(_EVALUATION_DIR, "environment", ".env"), override=True)

from jiuwen_memory.api import assemble  # noqa: E402
# NOTE: 引擎已重构掉检索时延打点，common.latency_collector 不再存在。下方
# retrieval_subprocess_latency_v5*.json 不再生成（引擎侧无打点数据可取）。
# LatencyCollector  # noqa: E402  (已移除: 模块不存在)
from jiuwen_memory.common.type_def import ChatMessage, Context, Scope  # noqa: E402
from jiuwen_memory.config import Config  # noqa: E402
from jiuwen_memory.common.security.types import AuthContext, RequestSecurityContext  # noqa: E402  (F05: identity 改为 security 上下文)
from evaluation.shared.config_loader import load_layer  # noqa: E402  (evaluation 内独立配置加载)
from jiuwen_memory.retrieval.types import DisclosureLevel  # noqa: E402  (测试参考.md: recall 走 L2 完整内容)

from evaluation.locomo import prompts  # noqa: E402  (远端 LoCoMo generate + judge prompt)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("v5")
logging.getLogger("agent_memory").setLevel(logging.WARNING)  # 引擎内部日志降级

# ---- 配置 --------------------------------------------------------------- #
# 检索深度：召回条数（对齐 mem0 locomo benchmark 召回 200）。每路召回宽度受 config.yml
# recall_max 封顶（已对齐提到 200：每路召回 200 → 融合池 400 → 取 RECALL_TOP_K=200）。
RECALL_TOP_K = int(os.getenv("RECALL_TOP_K", "200"))
# 多 cutoff 评测（对齐 mem0 locomo benchmark 默认 10,20,50,200）：召回一次 RECALL_TOP_K 条，
# 按 cutoff 切片各自生成+判分，得到 top_k 准确率曲线。逗号分隔；设单值退化为单点评测。
# 默认补齐 10,20：top10/top20 测浅召回（命中须在前几条的题），top50/top200 测深召回。
CUTOFFS = [int(c.strip()) for c in os.getenv("CUTOFFS", "10,20,50,200").split(",") if c.strip()]
# 抽取零产出时补写原文：metadata 去掉 infer 后走引擎默认路径（classifier 打标 → 落 KV →
# 建索引），保证该 turn 至少有一条可检索单元。全量约 50% 的 turn 抽取零产出，其原文原本
# 只落 /messages/ 且不建索引，检索永远够不着。
# 默认已由 1 改为 0：配合重写后的抽取 prompt（含时间锚点与具体值保留，零产出率应下降），
# 关掉兜底以测「纯抽取质量」——兜底会把抽取没捞到的原文捞回索引，掩盖抽取失效。
# 需要复现旧行为时显式设 RAW_FALLBACK=1。
RAW_FALLBACK = os.getenv("RAW_FALLBACK", "0").strip().lower() not in ("", "0", "false", "no", "off")
# 每个 turn 都额外写一份原文（与 RAW_FALLBACK 互斥的另一方案，A/B 用）。置 1 时覆盖前者。
RAW_ALWAYS = os.getenv("RAW_ALWAYS", "0").strip().lower() in ("1", "true", "yes", "on")
# 正文是否带绝对日期前缀，置 1 后正文形如 "[2023-05-07] [user]: ..."。
#
# 默认已由 1 改回 0，与抽取 prompt 保持一致：prompt 的时间规则现在明确写「源文本行不带
# 日期，基准取 user prompt 顶部的 observation_date 行」（见 llm_extractor.py 的
# _EXTRACT_SYSTEM_PROMPT），该行由 _llm_extract_batch 从 unit.metadata 注入，与本开关无关。
# 置 1 会让正文里也出现日期，与 prompt 的描述矛盾，只应在复现旧轮次时使用。
DATE_PREFIX = os.getenv("DATE_PREFIX", "0").strip().lower() not in ("", "0", "false", "no", "off")
# 关闭思维链。注入点在 _patched_openai_chat（类级 patch，覆盖引擎内算子与脚本 generate/judge），
# 注入 extra_body.chat_template_kwargs.enable_thinking=false。
# ⚠️ 默认关闭（=0）：该参数是 vLLM/自建 GLM-5.2 部署用的（GLM 默认输出思维链，需此关）。
# 华为云 MaaS 等 OpenAI 兼容托管端点不识别它，传了会触发慢路径——实测单次调用 53s vs 不传 5.9s，
# 慢 9 倍。仅当用自建 vLLM GLM 时设 1。DeepSeek-V3.2 无思维链问题，不要开。
DISABLE_THINKING = os.getenv("DISABLE_THINKING", "0").strip().lower() not in ("", "0", "false", "no", "off")
# 外层 conv 并发度（默认 10）：10 个 conv 同时跑，每 conv 内部 cutoff/qa 全程串行。
# 峰值 LLM 并发 = CONCURRENCY（10）。华为云 DeepSeek TPM 500000 实测 10 并发重试可兜，
# 20 并发（CUTOFF_INNER_CONCURRENCY=2）429 泛滥（0804 全量 214 次 RateLimit，0806 4407 次）。
CONCURRENCY = int(os.getenv("CONCURRENCY", "10"))
START_CONV = int(os.getenv("START_CONV", "0"))
MAX_QA = int(os.getenv("MAX_QA", "2"))  # 每 conv 跑 N 道 QA；0=全量
MAX_TURNS = int(os.getenv("MAX_TURNS", "5"))  # 每 session 取前 N 轮 turn；0=全量
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "0"))  # 每 conv 取前 N 个 session；0=全量
RESUME = os.getenv("RESUME", "0").strip().lower() not in ("", "0", "false", "no", "off")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))

data_path = os.getenv("INPUT_DATA_FILE", "")
if not data_path:
    data_path = os.path.join(_EVALUATION_DIR, "datasets", "locomo", "locomo10.json")

# ---- 单轮跑批的产物目录 --------------------------------------------------- #
# 默认每轮产物进 locomo/runs/<时间戳>_<RUN_TAG>/，而不是直接落在 locomo/ 根目录。
# 起因：test_response / test_recall / test_errors / test_mem_extract 四类文件由
# _append_jsonl 以 "a" 模式写入，跨轮次不清空。若都落在同一目录，第二轮的数据会与上一轮
# 混在同一文件里：错题文件尤其致命——据其筛出的题集与任何一轮都对不上。
#
# 覆盖方式（按优先级）：
#   OUTPUT_DIR=<dir>  完全指定产物目录，不创建 runs 子目录（沿用旧行为）
#   RUN_TAG=<tag>     本轮标签，进目录名，用于区分实验组；默认 v1
_RUNS_ROOT = os.getenv("RUNS_ROOT", os.path.join(_EVALUATION_DIR, "outputs", "locomo"))
# RUN_TAG 同时决定写入 scope 的 org（locomo-infer-<RUN_TAG>，见 ConversationProcessor.__init__）。
# 默认值取 "v1" 而非 "run"：原代码在该处的默认值即 "v1"，改默认值会使此前用默认参数写入的
# 记忆（org=locomo-infer-v1）在新一轮中读不到。
RUN_TAG = os.getenv("RUN_TAG", "v1").strip() or "v1"
# SCOPE_TAG 把「产物目录名」与「写入 scope」解耦，默认取 RUN_TAG（行为不变）。
# 检索侧 A/B 需要二者分离（换产物目录、复用同一批已写入记忆）时显式设为写入那轮的 RUN_TAG。
SCOPE_TAG = os.getenv("SCOPE_TAG", "").strip() or RUN_TAG
# 跳过写入、复用后端已有记忆。检索侧对比实验用：写入一次、反复跑检索。
# 要求 SCOPE_TAG 与写入那一轮一致，不一致会静默召回不到（org scope 对不上）。
SKIP_WRITE = os.getenv("SKIP_WRITE", "0").strip().lower() in ("1", "true", "yes", "on")
_explicit_out = os.getenv("OUTPUT_DIR", "").strip()
if _explicit_out:
    _out_dir = _explicit_out
    RUN_DIR = ""
else:
    RUN_DIR = os.path.join(_RUNS_ROOT, f"{time.strftime('%Y%m%d-%H%M%S')}_{RUN_TAG}")
    _out_dir = RUN_DIR
os.makedirs(_out_dir, exist_ok=True)

# 脚本日志同时落盘到轮次目录：终端输出会随窗口关闭丢失，事后复查一轮实验需要它。
# 引擎日志（engine.log）由 _build_config 的 globals.log_file 注入，单独落盘。
_run_log_path = os.path.join(_out_dir, "run.log")
try:
    _file_h = logging.FileHandler(_run_log_path, encoding="utf-8")
    _file_h.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] %(message)s",
                                            datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(_file_h)
except Exception as _e:  # 日志落盘失败不应中断跑批
    logger.warning("run.log 落盘失败(忽略): %s", str(_e)[:120])

_v5_suffix = "_v5"
result_path = os.getenv("RESULT_PATH", os.path.join(_out_dir, f"test_result{_v5_suffix}.json"))
metrics_path = os.getenv("METRICS_PATH", os.path.join(_out_dir, f"test_metrics{_v5_suffix}.json"))
# 检索时延拆分（闭合阶段墙钟 + 并行段细项 + 闭合校验）：引擎内 LatencyCollector
# 单例打点，跑批末尾取 snapshot 落盘。与 test_metrics.json 解耦——后者是端到端阶段
# (add_mem/search/generate/validate)，本文件专记检索链路内部时延结构与闭合性。
retrieval_latency_path = os.getenv(
    "RETRIEVAL_LATENCY_PATH",
    os.path.join(_out_dir, f"retrieval_subprocess_latency{_v5_suffix}.json"),
)

# ---- 产物落盘路径（按 conv 分文件，对齐参考实现 D:\project\openjiuwen\test\agent-memory\locomo\test_locomo_v5.py）----
# 提取的记忆：write 阶段每批 api.write 返回的派生 MemoryUnit 序列化落盘（test_mem_extract_v5<conv>.jsonl）
mem_extract_path = os.getenv("MEM_EXTRACT_PATH", os.path.join(_out_dir, f"test_mem_extract{_v5_suffix}"))
# 生成的 response：每题 qa_idx/question/answer/response/category 落盘（test_response_v5<conv>.json）
response_path = os.getenv("RESPONSE_PATH", os.path.join(_out_dir, f"test_response{_v5_suffix}"))
# 召回的记忆：generate 阶段每题召回的 RetrievedItem 列表 + created_at（test_recall_v5<conv>.jsonl）
recall_detail_path = os.getenv("RECALL_DETAIL_PATH", os.path.join(_out_dir, f"test_recall{_v5_suffix}"))
# 错题统计：validate 阶段只记错题（召回记忆+response+judge 输出）（test_errors_v5<conv>.jsonl）
errors_detail_path = os.getenv("ERRORS_DETAIL_PATH", os.path.join(_out_dir, f"test_errors{_v5_suffix}"))
# 错题详情开关（默认开）：保存每题召回记忆+对错+judge 输出，便于排查。设 SAVE_ERROR_DETAIL=0 关闭。
SAVE_ERROR_DETAIL = os.getenv("SAVE_ERROR_DETAIL", "1").strip().lower() not in ("", "0", "false", "no", "off")

# LoCoMo category → 名称（对齐 prompts.CATEGORY_NAMES，但用中文友好键）
_CAT_NAMES = {1: "Multi-hop", 2: "Temporal", 3: "Open-domain", 4: "Single-hop"}

_DATE_FMT = "%I:%M %p on %d %B, %Y"  # "1:56 pm on 8 May, 2023"

# =====================================================================
# token + 时延统计（monkey-patch OpenAILLM.chat 旁路捕获 usage）
# =====================================================================
# 新架构 LLM 入口是 OpenAILLM.chat，内部 self.client.chat.completions.create，
# response.usage 有 prompt_tokens/completion_tokens/total_tokens（后端不返回时为 None）。
# patch 类方法覆盖引擎内全部 LLM 调用（classifier 抽取 / generate / judge）。
from jiuwen_memory.common.llm.llm_impl.openai_llm import OpenAILLM  # noqa: E402

# 统计块结构:[invoke次数, 总耗时秒, input_tokens, output_tokens, total_tokens]
_PHASE_FIELDS = ("add_mem", "search", "generate", "validate")
phase_metrics: dict[int, dict] = {}
# 每次 phase 调用的单独耗时（秒），按阶段聚合跨 conv 分布 → 算 p50/p90/p95
phase_latencies: dict[str, list[float]] = {f: [] for f in _PHASE_FIELDS}
# 多 cutoff 并发下，多个 worker 线程同时写 phase_metrics/phase_latencies（read-modify-write），
# 需加锁防累加竞争导致统计错乱。GIL 只保证单字节码原子，不保证 += 的 RMW 整体原子。
import threading as _threading
_metrics_lock = _threading.Lock()
_current_conv_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar("conv_id", default=None)
_current_phase_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("phase", default=None)


def _new_block() -> dict:
    return {f: [0, 0.0, 0, 0, 0] for f in _PHASE_FIELDS}


def _percentile(sorted_vals: list[float], p: float) -> float:
    """已升序排列的样本上取分位数（p∈[0,100]，用最近秩法）。空样本返回 0.0。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 4)
    # 最近秩法：rank = ceil(p/100 * n) - 1，夹在 [0, n-1]
    rank = max(0, min(len(sorted_vals) - 1, int(p / 100.0 * len(sorted_vals) + 0.5) - 1))
    return round(sorted_vals[rank], 4)


def _ser_unit(unit) -> dict:
    """把 MemoryUnit（dataclass）序列化成可 JSON 落盘的 dict。

    含 Enum 字段（tier/lifecycle 等）转成 .value；temporal/layers 等嵌套 dataclass
    递归降级到基本类型。供提取记忆落盘用（参考实现 _dump_extracted_memories 的 asdict 等价）。

    注：``content`` 是 property（从 segments 派生，非 dataclass 字段），``dataclasses.fields``
    遍历不到 → 单独显式取，否则产物文件里记忆正文会缺失。segments 正文同样显式拼接便于直读。
    """
    import dataclasses
    from enum import Enum

    def _conv(v):
        if isinstance(v, Enum):
            return v.value
        if dataclasses.is_dataclass(v):
            return {f.name: _conv(getattr(v, f.name)) for f in dataclasses.fields(v)}
        if isinstance(v, list):
            return [_conv(x) for x in v]
        if isinstance(v, dict):
            return {k: _conv(x) for k, x in v.items()}
        # datetime 等不可直接 json 序列化的对象转 str
        if v is not None and not isinstance(v, (str, int, float, bool, type(None))):
            return str(v)
        return v

    if not dataclasses.is_dataclass(unit):
        return {"content": str(unit)}
    out = {f.name: _conv(getattr(unit, f.name)) for f in dataclasses.fields(unit)}
    # 显式补 content（property，运行时从 segments 派生）+ segments 正文拼接
    try:
        out["content"] = _conv(getattr(unit, "content", None))
    except Exception:
        pass
    try:
        segs = getattr(unit, "segments", None) or []
        out["segments_content"] = " || ".join(
            str(getattr(s, "content", "")) for s in segs
        ).strip()
    except Exception:
        out["segments_content"] = ""
    return out


def _append_jsonl(path: str, record: dict, conv_id: int) -> None:
    """按 conv 追加写一行 JSON（失败仅 warning，不阻断主流程）。"""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("conv %d 写 %s 失败(忽略): %s", conv_id, path, str(e)[:120])


def _patched_openai_chat(self, messages, **options):
    """重写 OpenAILLM.chat：复制原逻辑 + 从 response.usage 旁路记 token。

    注：耗时与 invoke_count 由 _PhaseTimer 统一记（避免与计时器重复累加导致耗时翻倍）；
    此处只负责 token 旁路统计。
    """
    conv_id = _current_conv_ctx.get()
    phase = _current_phase_ctx.get()
    track = conv_id is not None and phase is not None

    import openai as _openai  # noqa
    oa_messages = [{"role": m.role, "content": m.content} for m in messages]
    temperature = options.get("temperature", self._default_temperature)
    max_tokens = options.get("max_tokens", self._default_max_tokens)
    # OpenAILLM 属性名随版本变动（旧 _model_name，新 _fallback_model），
    # 经 _endpoint() 统一解析拿当前生效 model/client，不读私有属性以免漂移。
    _ep = self._endpoint()
    create_kwargs: dict = {
        "model": _ep.model,
        "messages": oa_messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    self._merge_request_options(create_kwargs, options)
    # DISABLE_THINKING：关掉模型的思维链。OpenAILLM 的 producer（openai_llm.py 的 _build）
    # 只接 llm_model/llm_base_url/llm_api_key/llm_temperature/llm_max_tokens 五个键，不接
    # extra_body，故 config.yml 无法注入该字段。本函数是类级 monkey-patch（见文件末尾
    # OpenAILLM.chat = _patched_openai_chat），覆盖引擎内 extractor/classifier/dedup 与脚本
    # generate/judge 的全部调用，是唯一能统一注入的位置。不关的后果：抽取 prompt 要求只输出
    # JSON 数组，思维链会在 JSON 前附一大段推理，解析失败即候选全丢——放大零产出率。
    # 已显式传入 extra_body 的调用方优先（合并而非覆盖），与 _merge_request_options 一致。
    if DISABLE_THINKING:
        _eb = create_kwargs.get("extra_body")
        _tpl = {"chat_template_kwargs": {"enable_thinking": False}}
        create_kwargs["extra_body"] = ({**_tpl, **_eb} if isinstance(_eb, dict) else _tpl)
    # 华为云 MaaS 限流（429 RateLimitError）+ 连接/超时/5xx 等瞬态错误退避重试。
    # 覆盖全部 OpenAILLM.chat 调用：引擎内 extractor/classifier + v5 generate/judge。
    # LLMClassifier 自带通用重试兜底其他异常；此处专治瞬态错误，避免 429 把 QA 直接打废。
    _TRANSIENT = (_openai.RateLimitError, _openai.APIConnectionError,
                  _openai.APITimeoutError, _openai.InternalServerError)
    response = None
    for _attempt in range(4):  # 1 次正常 + 3 次重试
        try:
            response = self.client.chat.completions.create(**create_kwargs)
            break
        except _TRANSIENT as exc:
            if _attempt == 3:
                logger.error("LLM 瞬态错误重试耗尽: %s: %s", type(exc).__name__, str(exc)[:120])
                raise
            _wait = min(1.0 * (2 ** _attempt), 16.0)  # 1/2/4s，封顶 16s
            logger.warning("LLM 瞬态错误 %s，%.1fs 后重试（attempt %d/3）",
                           type(exc).__name__, _wait, _attempt + 1)
            time.sleep(_wait)
        except _openai.APIError as exc:
            logger.error("OpenAILLM API error: %s", exc)
            raise
    content = response.choices[0].message.content or ""

    if track:
        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
            out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
            # 多 cutoff 并发下多线程写共享统计，加锁防 RMW 竞争
            with _metrics_lock:
                block = phase_metrics.setdefault(conv_id, _new_block())[phase]
                block[2] += in_tok
                block[3] += out_tok
                block[4] += in_tok + out_tok
    return content


OpenAILLM.chat = _patched_openai_chat


class _PhaseTimer:
    """阶段计时上下文：进入 set phase ContextVar，退出把耗时写入对应 conv/phase 块。"""

    def __init__(self, phase: str, conv_id: int):
        self.phase = phase
        self.conv_id = conv_id
        self._start = None
        self._token = None
        self._phase_token = None

    def __enter__(self):
        self._token = _current_conv_ctx.set(self.conv_id)
        if self.phase in ("add_mem", "generate", "validate"):
            self._phase_token = _current_phase_ctx.set(self.phase)
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = time.perf_counter() - self._start
        # 多 cutoff 并发下多线程写共享统计，加锁防 RMW 竞争
        with _metrics_lock:
            block = phase_metrics.setdefault(self.conv_id, _new_block())[self.phase]
            block[0] += 1   # invoke_count：每次阶段调用计一次（含不调 LLM 的 search）
            block[1] += elapsed
            # 追加单次耗时到 per-phase 全局列表（用于 p50/p90/p95 分布统计）
            phase_latencies[self.phase].append(elapsed)
        _current_conv_ctx.reset(self._token)
        if self._phase_token is not None:
            _current_phase_ctx.reset(self._phase_token)
        return False


# =====================================================================
# 装配配置：加载对齐后的 locomo/config.yml（真后端 Redis+Milvus+ES）
# =====================================================================
# 与之前手搓字典不同：改用 locomo/config.yml，与 staged eval(run_locomo_staged_eval)
# 及 smoke_concurrent 同一份配置源，确保 v5 / staged / smoke 三套脚本口径一致。
# config.yml 里 ${VAR} 经 load_layer 的 expand_env 展开（值来自 locomo/.env，已由
# load_dotenv 载入 os.environ）。config.yml 已含：llm=openai(DeepSeek)、embedder=openai(bge-m3)、
# tokenizer=jieba、kv_store=redis、fulltext_store=elasticsearch、vector_store=milvus、
# 构建侧 extractor/abstractor/associator/classifier 全走 llm、关 reranker。
# CONFIG_FILE 可覆盖为其他配置文件，用于换一份配置复跑。相对路径按本目录解析。
_cfg_env = os.getenv("CONFIG_FILE", "").strip()
CONFIG_PATH = (
    _cfg_env if os.path.isabs(_cfg_env)
    else os.path.join(_TEST_DIR, _cfg_env) if _cfg_env
    else os.path.join(_TEST_DIR, "config.yml")
)


def _build_config() -> Config:
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError(f"配置文件不存在: {CONFIG_PATH}")
    layer = load_layer(CONFIG_PATH)
    # config.yml 顶层是 profile + memory_api 两键，内核装配取 memory_api 段
    payload = dict(layer.get("memory_api") or layer)

    # 引擎日志落到本轮目录：agent_memory 根 logger 由 setup_logging 装自己的 handler 并置
    # propagate=False，不会流入脚本侧 run.log，只能经 log_file 参数单独指定。写进 globals
    # 而非某组件 params：setup_logging 用 ComponentSpec.get 读取，实例 params 缺失时回退
    # globals。不覆盖配置文件里已显式写死的 log_file。
    globals_ = dict(payload.get("globals") or {})
    globals_.setdefault("log_file", os.path.join(_out_dir, "engine.log"))
    payload["globals"] = globals_

    logger.info("[config] 加载 %s，命名空间: %s", CONFIG_PATH, sorted(payload.keys()))
    logger.info("[config] 引擎日志: %s", globals_["log_file"])

    # 配置快照：落原始 YAML 文本而非展开后的字典——展开后的值含 ${LLM_API_KEY} 等密钥，
    # 落盘即把密钥写进实验产物；原文只有变量名，可安全随产物保留与分享。
    try:
        import shutil

        shutil.copyfile(CONFIG_PATH, os.path.join(_out_dir, "config.snapshot.yml"))
    except Exception as e:  # 快照失败不应中断跑批
        logger.warning("配置快照落盘失败(忽略): %s", str(e)[:120])

    return Config.from_dict(payload)


# =====================================================================
# 单轮跑批的元信息与收尾
# =====================================================================
# 记录到 run_meta.json 的环境变量白名单。用白名单而非「导出全部 os.environ」：后者会把
# API_KEY / MODEL_API_TOKEN 等密钥写进实验产物，而产物是要随结果分享和长期保留的。
_META_ENV_KEYS = (
    "RUN_TAG", "SCOPE_TAG", "SKIP_WRITE", "CONV_IDS", "START_CONV", "CONCURRENCY",
    "MAX_QA", "MAX_TURNS", "MAX_SESSIONS", "RECALL_TOP_K", "CUTOFFS",
    "RESUME", "RUN_DIR", "OUTPUT_DIR",
    "RAW_FALLBACK", "RAW_ALWAYS", "DATE_PREFIX",
    "SAVE_ERROR_DETAIL", "LLM_TIMEOUT", "DISABLE_THINKING",
    # 后端与模型标识：非密钥，用于事后辨认这轮跑在哪个后端、哪个模型上
    "LLM_MODEL", "LLM_BASE_URL", "EMBED_MODEL_NAME", "EMBED_BASE_URL",
    "REDIS_URL", "ES_HOSTS", "ES_FULLTEXT_INDEX", "MILVUS_URI", "MILVUS_COLLECTION",
)
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _git_commit() -> Optional[str]:
    """取当前仓库 HEAD 的短 commit，失败返回 None（非 git 环境或未安装 git 都不应中断跑批）。

    daily_runner 跑测试前已 fetch+rebase，HEAD 是 rebase 后的顶端（含本 fork 独有 commit，
    如 locomo 测试改动）——hash 跟 upstream/mem2.0 不同。手动跑测试时 HEAD 是当前
    checkout 分支的顶端。要拿主仓 mem2.0 当时最新，看 _upstream_commit()。
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", _PROJECT_ROOT, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _upstream_commit() -> Optional[str]:
    """取 upstream/mem2.0 远端跟踪 ref 的短 commit，失败返回 None。

    跟 daily_runner config.yml 的 repo.remote=upstream、repo.branch=mem2.0 默认值一致。
    daily_runner 跑测试前已 git fetch upstream mem2.0，此 ref 是主仓当时最新。
    手动跑测试时若没 fetch 过，此 ref 是上次 fetch 的旧值——想要真·主仓最新，
    跑测试前手动 git fetch upstream mem2.0，或直接用 daily_runner 跑。
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", _PROJECT_ROOT, "rev-parse", "--short", "upstream/mem2.0"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _file_digest(path: str) -> Optional[str]:
    """数据集文件的 sha256 前 16 位。事后核对 qa_idx 是否仍指同一批题。"""
    import hashlib

    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:
        return None


def _dump_run_meta(todo: list, data_file: str) -> None:
    """把本轮的参数、题集、代码版本落到 run_meta.json。失败仅告警，不中断跑批。"""
    env_snapshot = {}
    for key in _META_ENV_KEYS:
        if any(marker in key.upper() for marker in _SECRET_MARKERS):
            continue  # 双保险：白名单里若误加了密钥类键名，这里再挡一次
        value = os.environ.get(key)
        if value is not None:
            env_snapshot[key] = value

    # 实际生效值。env 段只记 os.environ 里存在的键，未显式设置、走默认值的参数不会出现在
    # 那里——但它们同样影响结果（RAW_FALLBACK / DATE_PREFIX 默认即启用）。只看 env 段会
    # 误判成「这些没开」，事后无法复现。故另记一份解析后的实际取值。
    effective = {
        "RECALL_TOP_K": RECALL_TOP_K,
        "CUTOFFS": CUTOFFS,
        "MAX_QA": MAX_QA,
        "MAX_TURNS": MAX_TURNS,
        "MAX_SESSIONS": MAX_SESSIONS,
        "CONCURRENCY": CONCURRENCY,
        "RAW_FALLBACK": RAW_FALLBACK,
        "RAW_ALWAYS": RAW_ALWAYS,
        "DATE_PREFIX": DATE_PREFIX,
        "DISABLE_THINKING": DISABLE_THINKING,
        "SAVE_ERROR_DETAIL": SAVE_ERROR_DETAIL,
        "RESUME": RESUME,
        "RUN_TAG": RUN_TAG,
        "SCOPE_TAG": SCOPE_TAG,
        "scope_org": f"locomo-infer-{SCOPE_TAG}",
        "SKIP_WRITE": SKIP_WRITE,
    }

    meta = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "run_tag": RUN_TAG,
        "effective": effective,
        "run_dir": RUN_DIR or _out_dir,
        "git_commit": _git_commit(),
        "upstream_commit": _upstream_commit(),
        "config_path": CONFIG_PATH,
        "data_file": data_file,
        "data_sha256_16": _file_digest(data_file),
        "conv_todo": todo,
        "env": env_snapshot,
    }
    try:
        with open(os.path.join(_out_dir, "run_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("run_meta 落盘失败(忽略): %s", str(e)[:120])


def _finalize_run_dir() -> None:
    """收尾：打印产物清单，并把本轮目录写入 runs/latest.txt。"""
    try:
        names = sorted(os.listdir(_out_dir))
    except Exception as e:
        logger.warning("产物目录读取失败(忽略): %s", str(e)[:120])
        return

    logger.info("产物目录: %s", _out_dir)
    for name in names:
        full = os.path.join(_out_dir, name)
        if os.path.isfile(full):
            logger.info("  %-34s %8.1f KB", name, os.path.getsize(full) / 1024)

    # OUTPUT_DIR 模式下产物不在 runs 下，写 latest 会指向一个与 runs 无关的路径，故跳过
    if not RUN_DIR:
        return
    try:
        os.makedirs(_RUNS_ROOT, exist_ok=True)
        with open(os.path.join(_RUNS_ROOT, "latest.txt"), "w", encoding="utf-8") as fh:
            fh.write(_out_dir + "\n")
    except Exception as e:
        logger.warning("latest.txt 写入失败(忽略): %s", str(e)[:120])


# =====================================================================
# 工具
# =====================================================================
def _parse_judge_json(content: str) -> str:
    """从 judge 输出抽 label（容忍 markdown fence/前后文字）。"""
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        try:
            label = str(json.loads(text[start:end + 1]).get("label", "")).strip().upper()
            if label:
                return label
        except json.JSONDecodeError:
            pass
    # 兜底子串判定
    if "CORRECT" in text.upper() and "WRONG" not in text.upper():
        return "CORRECT"
    if "WRONG" in text.upper():
        return "WRONG"
    return ""


def _infer_reference_date(conv: dict) -> str:
    """从对话第一个能 parse 的 session_date_time 推断 reference_date → 'Month DD, YYYY'。"""
    conv_data = conv.get("conversation", {})
    for k in sorted(conv_data.keys()):
        if k.startswith("session_") and k.endswith("_date_time"):
            dt = _parse_dt(conv_data.get(k, ""))
            if dt:
                return dt.strftime("%B %d, %Y")
    return "2023"


def _parse_dt(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str.strip(), _DATE_FMT)
    except (ValueError, AttributeError):
        return None


def _session_num(key: str) -> int:
    return int(key.split("_")[1])


# =====================================================================
# 单 conversation 处理（worker 线程内执行，同步 API）
# =====================================================================
class ConversationProcessor:
    """单个 conversation 处理器（共享 api + scope 隔离）。"""

    def __init__(self, conv_id: int, api, llm):
        self.conv_id = conv_id
        self.api = api
        self.llm = llm  # 共享 OpenAILLM 实例（chat 已被 patch 记 token）
        # 用模块级 SCOPE_TAG 而非在此另做 os.getenv：两处各自取默认值会漂移，且
        # SKIP_WRITE 复用记忆（若将来启用）要求 scope 与写入那轮一致，不一致会静默召回不到。
        # SCOPE_TAG 默认等于 RUN_TAG。
        self.scope = Scope(org=f"locomo-infer-{SCOPE_TAG}", user=f"conv_{conv_id}")
        self.identity = self.scope  # 本人操作本人 scope
        # F05 重构后 add/search/inspect 用 security 上下文，从 auth.actor 取 identity
        self.security = RequestSecurityContext(auth=AuthContext(actor=self.identity))
        self._write_seq = 0  # conv 内 write 递增序号（对齐 staged ingest 的 write_id）

    def _write_raw(self, content: str, sk: str, metadata: dict, occurred):
        """按引擎默认路径写原文：system_metadata 去掉 infer 键后 Engine.write 走 classifier 打
        tier/tags → 落 KV → index_builder.build（默认路径分支），与 infer 抽取出的派生记忆
        共用同一个 IndexBuilder，因此写进去即可被 search 召回。

        用于抽取零产出时兜底（RAW_FALLBACK），保证该 turn 至少有一条可检索单元。
        tags 加 "raw" 便于 analyze_run 把兜底的贡献从抽取质量变化里分离出来。
        """
        raw_meta = {k: v for k, v in metadata.items() if k != "infer"}
        raw_meta["raw_fallback"] = "true"
        return self.api.add(
            content, self.scope,
            security=self.security,
            tags=[sk, "raw"],
            system_metadata=raw_meta,
            occurred_at=occurred,
        )

    def write_conversation(self, conv: dict) -> int:
        """写入对话 turn（按 MAX_SESSIONS / MAX_TURNS 截断）。返回写入条数。"""
        conv_data = conv.get("conversation", {})
        speaker_a = conv_data.get("speaker_a", "")
        speaker_b = conv_data.get("speaker_b", "")
        session_keys = sorted(
            (k for k in conv_data if k.startswith("session_") and not k.endswith("_date_time")),
            key=_session_num,
        )
        if MAX_SESSIONS > 0:
            session_keys = session_keys[:MAX_SESSIONS]
        n = 0
        for sk in session_keys:
            base_dt = _parse_dt(conv_data.get(f"{sk}_date_time", ""))
            chats = conv_data[sk]
            if MAX_TURNS > 0:
                chats = chats[:MAX_TURNS]
            for turn, chat in enumerate(chats, start=1):
                text = chat.get("text", "")
                if chat.get("blip_caption"):
                    text += f"(The conversation is accompanied by an image, and the description of the image is '{chat['blip_caption']}')"
                # role 映射：speaker_a→user, speaker_b→assistant（与 prompt "User=main person" 对齐）
                text = text.replace(speaker_a, "user").replace(speaker_b, "assistant")
                if not text.strip():
                    continue
                role = "user" if chat.get("speaker") == speaker_a else "assistant"
                # DATE_PREFIX：把 session 日期写进正文。抽取 prompt 要求用 observation_date
                # 把 "yesterday"/"last week" 解析成绝对日期，但 observation_date 只在
                # metadata 里，正文没有锚点，导致相对时间常常原样留在记忆里。
                _obs = base_dt.strftime("%Y-%m-%d") if base_dt else ""
                content = (f"[{_obs}] [{role}]: {text}"
                           if (DATE_PREFIX and _obs) else f"[{role}]: {text}")
                occurred = (base_dt + timedelta(seconds=turn)) if base_dt else None
                # 对齐 locomo.md staged ingest 的 metadata 口径：
                # - infer="true" 触发 Engine.write 走 Evolver(EXTRACT) → LLM Extractor 抽取
                #   （否则走默认路径只 classifier 打 tier/tags + 建索引，不抽记忆）
                # - seed_key/write_id/observation_date 供诊断追溯（对齐 _write_seed）
                self._write_seq += 1
                seed_key = f"conv_{self.conv_id}/D{_session_num(sk)}:{turn}"
                write_id = f"write-{self._write_seq:06d}-{uuid.uuid4().hex[:8]}"
                metadata = {
                    "speaker": chat.get("speaker", ""),
                    "session": sk,
                    "seed_key": seed_key,
                    "write_id": write_id,
                    "observation_date": (base_dt.strftime("%Y-%m-%d") if base_dt else ""),
                    "infer": "true",
                }
                with _PhaseTimer("add_mem", self.conv_id):
                    try:
                        derived = self.api.add(
                            content, self.scope,
                            security=self.security,
                            tags=[sk],
                            system_metadata=metadata,
                            occurred_at=occurred,
                        )
                        n += 1
                        # 原文兜底：抽取零产出的 turn 占全量约 50%，其原文原本只落
                        # /messages/ 且不建索引，检索永远够不着。补一次默认路径写入
                        # 把它捞回索引。api.add 返回派生 MemoryUnit 列表，
                        # derived 为空有三种成因（抽取返回 [] / UPDATE 吸收 / NOOP），
                        # 只有第一种是信息真的没进库；另两种下兜底是冗余但无害。
                        raw_units = []
                        if RAW_ALWAYS or (RAW_FALLBACK and not derived):
                            raw_units = self._write_raw(content, sk, metadata, occurred)
                        # 落盘条件去掉 `and derived`：零产出 turn 也记一行，否则日志里
                        # 完全看不见它们（只能与数据集做差集反推）。analyze_run 据此算
                        # 零产出率与 RAW_FALLBACK 的干净贡献。
                        if SAVE_ERROR_DETAIL:
                            _append_jsonl(
                                f"{mem_extract_path}{self.conv_id}.jsonl",
                                {
                                    "conv_id": self.conv_id,
                                    "session": sk,
                                    "turn": turn,
                                    "write_id": write_id,
                                    "occurred_at": (occurred.isoformat() if occurred else ""),
                                    "source_content": content,
                                    "derived_count": len(derived),
                                    "raw_count": len(raw_units),
                                    "memories": ([_ser_unit(u) for u in derived]
                                                 + [_ser_unit(u) for u in raw_units]),
                                },
                                self.conv_id,
                            )
                    except Exception as e:
                        logger.warning("conv %d write turn %s failed: %s",
                                       self.conv_id, sk, str(e)[:120])
        logger.info("conv %d 写入完成: %d turns (max_sessions=%s max_turns=%s)",
                    self.conv_id, n,
                    MAX_SESSIONS or "全量", MAX_TURNS or "全量")
        return n

    def recall(self, question: str, qa_idx: int | None = None) -> tuple[list, dict]:
        """召回记忆一次（top_k=RECALL_TOP_K）。返回 (search_results, recall_record)。

        search_results 是 [{memory, created_at}] 列表，供多 cutoff 切片复用。
        recall_record 含每条召回记忆的 content/created_at/score/level + 检索轨迹，
        落盘到 test_recall_v5<conv>.jsonl（每题一次，与 cutoff 数无关）。
        """
        with _PhaseTimer("search", self.conv_id):
            try:
                res = self.api.search(
                    question, Context(self.scope),
                    security=self.security, top_k=RECALL_TOP_K,
                    disclosure=DisclosureLevel.L2,  # 测试参考.md: 返回完整 MemoryUnit 内容
                    with_trajectory=True,          # 测试参考.md: 返回召回阶段轨迹
                )
            except Exception as e:
                logger.warning("conv %d recall failed: %s", self.conv_id, str(e)[:120])
                res = None
        items = (res.items if res else [])
        # 转成 prompts.get_answer_generation_prompt 要的 [{memory, created_at}]
        # recall 返回的 RetrievedItem 不暴露 temporal，created_at 取不到 → prompt 全显
        # "(unknown date)"，时间题缺锚点。用 inspect 按 unit_id 批量反查完整 MemoryUnit，
        # 取 unit.temporal.t_event（即 write 时传入的 occurred_at，session 事件时间）作为 created_at。
        created_at_map: dict[str, str] = {}
        if items:
            unit_ids = [it.unit_id for it in items if it.unit_id]
            if unit_ids:
                try:
                    units = self.api.inspect(unit_ids, self.scope, security=self.security)
                    for u in units:
                        te = getattr(getattr(u, "temporal", None), "t_event", None)
                        if te is not None:
                            created_at_map[u.id] = te.isoformat()
                except Exception as e:
                    logger.warning("conv %d inspect 反查时间失败: %s",
                                   self.conv_id, str(e)[:120])
        search_results = [
            {"memory": it.content,
             "created_at": created_at_map.get(it.unit_id, "")}
            for it in items
        ]
        recall_record = {
            "conv_id": self.conv_id,
            "qa_idx": qa_idx,
            "query": question,
            "recall_count": len(items),
            "recalled_memories": [
                {
                    "idx": i,
                    "unit_id": it.unit_id,
                    "score": round(it.score, 4),
                    "level": it.level.value,
                    "created_at": created_at_map.get(it.unit_id, ""),
                    "content": it.content,
                }
                for i, it in enumerate(items)
            ],
            "trajectory": [
                {
                    "stage": s.stage,
                    "channel": (s.channel.value if s.channel else ""),
                    "n": s.candidate_count,
                    "cost_ms": round(s.cost_ms, 2),
                    "detail": s.detail,
                }
                for s in (res.trajectory if res else [])
            ],
        }
        if SAVE_ERROR_DETAIL and qa_idx is not None:
            _append_jsonl(f"{recall_detail_path}{self.conv_id}.jsonl",
                          recall_record, self.conv_id)
        return search_results, recall_record

    def answer_and_judge(self, question: str, gold: str, category: int,
                         search_results: list, reference_date: str,
                         cutoff: int) -> tuple[str, bool, str, str]:
        """对单 cutoff 切片生成回答 + 判分。

        切 search_results[:cutoff] → generate prompt → LLM 生成 → judge prompt → 判分。
        返回 (response, ok, label, verdict)。对齐 mem0 locomo 的 _run_one_cutoff。
        """
        sliced = search_results[:cutoff]
        prompt = prompts.get_answer_generation_prompt(
            question=question, search_results=sliced,
            reference_date=reference_date,
        )
        with _PhaseTimer("generate", self.conv_id):
            response = self.llm.chat([ChatMessage(role="user", content=prompt)],
                                      temperature=0.01, max_tokens=2048)
        judge_prompt = prompts.get_judge_prompt(category, question, gold, response)
        with _PhaseTimer("validate", self.conv_id):
            verdict = self.llm.chat(
                [ChatMessage(role="system", content=prompts.JUDGE_SYSTEM_PROMPT),
                 ChatMessage(role="user", content=judge_prompt)],
                temperature=0.0, max_tokens=512,
            )
        label = _parse_judge_json(verdict)
        return response, (label == "CORRECT"), label, verdict

    def process_full(self, conv: dict) -> dict:
        """处理单个 conv 全流程：写→生成→判分。返回结果记录。"""
        logger.info("conv %d 开始: scope=%s", self.conv_id, self.scope.user)
        t0 = time.perf_counter()

        # 1) 写入对话 turn（按 MAX_SESSIONS/MAX_TURNS 截断，默认前 1 session 前 5 轮）
        # SKIP_WRITE=1 时复用后端已有记忆，跳过写入：对比实验换产物目录、不重跑写入，
        # 避免抽取随机性造成的题级翻转淹没待测改动信号。要求 SCOPE_TAG 与写入轮一致。
        if SKIP_WRITE:
            logger.info("conv %d SKIP_WRITE=1，复用后端已有记忆，跳过写入", self.conv_id)
        else:
            self.write_conversation(conv)

        # 2) 推断 reference_date
        reference_date = _infer_reference_date(conv)

        # 3) 遍历 QA：召回一次 → 多 cutoff 切片各自生成+判分（对齐 mem0 locomo benchmark）
        speaker_a = conv.get("conversation", {}).get("speaker_a", "")
        speaker_b = conv.get("conversation", {}).get("speaker_b", "")
        # 按 cutoff 分桶统计：correct[cutoff][category] / total[cutoff][category]
        correct = {c: defaultdict(int) for c in CUTOFFS}
        total = {c: defaultdict(int) for c in CUTOFFS}
        skipped = 0  # 限流/瞬态错误重试耗尽导致的失败，不计入分母（非真错）
        qa_done = 0
        for q_idx, qa in enumerate(conv.get("qa", [])):
            category = qa.get("category", 0)
            if category > 4 or category < 1:
                continue
            if MAX_QA > 0 and qa_done >= MAX_QA:
                logger.info("conv %d MAX_QA=%d 已跑完", self.conv_id, MAX_QA)
                break
            qa_done += 1
            question = qa["question"].replace(speaker_a, "user").replace(speaker_b, "assistant")
            gold = prompts.preprocess_answer(category, str(qa.get("answer", "")))
            gold = gold.replace(speaker_a, "user").replace(speaker_b, "assistant")
            # 召回一次（与 cutoff 数无关，多 cutoff 复用同一批召回结果）
            try:
                search_results, recall_record = self.recall(question, qa_idx=q_idx)
                n_recall = recall_record["recall_count"]
            except Exception as e:
                # 限流/瞬态错误重试耗尽 → 不计分母（skip）；其他异常 → 计为真错
                import openai as _oa
                if isinstance(e, (_oa.RateLimitError, _oa.APIConnectionError,
                                  _oa.APITimeoutError, _oa.InternalServerError)):
                    skipped += 1
                    logger.warning("conv %d qa%d 限流 skip: %s", self.conv_id, q_idx, str(e)[:80])
                    continue  # 不计 correct/total，跳过此题
                logger.warning("conv %d qa%d recall failed: %s", self.conv_id, q_idx, str(e)[:120])
                search_results, recall_record, n_recall = [], {"recalled_memories": []}, 0
            # 多 cutoff 生成+判分：题内串行（各切 search_results[:c] 顺序跑 generate+validate）。
            # 外层 10 conv 并发，每 conv 内部 cutoff/qa 全程串行，峰值 LLM 并发 = CONCURRENCY。
            # 单 cutoff 限流时该 cutoff 不计分母，其余 cutoff 仍可统计（容错）。
            per_cutoff: dict[int, dict] = {}
            for cutoff in CUTOFFS:
                try:
                    response, ok, label, verdict = self.answer_and_judge(
                        question, gold, category, search_results, reference_date, cutoff)
                except Exception as e:
                    import openai as _oa
                    if isinstance(e, (_oa.RateLimitError, _oa.APIConnectionError,
                                      _oa.APITimeoutError, _oa.InternalServerError)):
                        logger.warning("conv %d qa%d cutoff %d 限流 skip: %s",
                                       self.conv_id, q_idx, cutoff, str(e)[:80])
                        continue  # 不计分母
                    logger.warning("conv %d qa%d cutoff %d failed: %s",
                                   self.conv_id, q_idx, cutoff, str(e)[:120])
                    response, ok, label, verdict = "", False, "", ""
                correct[cutoff][category] += 1 if ok else 0
                total[cutoff][category] += 1
                per_cutoff[cutoff] = {
                    "response": response, "ok": ok, "label": label, "verdict": verdict,
                }
                _append_jsonl(
                    f"{response_path}{self.conv_id}.json",
                    {"qa_idx": q_idx, "cutoff": cutoff, "category": category,
                     "question": question, "answer": gold, "response": response},
                    self.conv_id,
                )
                if SAVE_ERROR_DETAIL and not ok:
                    _append_jsonl(
                        f"{errors_detail_path}{self.conv_id}.jsonl",
                        {"conv_id": self.conv_id, "qa_idx": q_idx, "cutoff": cutoff,
                         "category": category, "question": question, "gold_answer": gold,
                         "response": response, "judge_label": label, "judge_verdict": verdict,
                         "recall_count": n_recall,
                         "recalled_memories": recall_record.get("recalled_memories", [])},
                        self.conv_id,
                    )
            # 取最大 cutoff 的判分作为该题日志展示（最贴近完整上下文口径）
            shown = per_cutoff.get(max(CUTOFFS), {})
            logger.info("conv %d qa%d cat%d %s (recall=%d, cutoffs=%s)",
                        self.conv_id, q_idx, category,
                        "✓" if shown.get("ok") else "✗", n_recall,
                        {c: "✓" if per_cutoff.get(c, {}).get("ok") else "✗" for c in CUTOFFS})

        elapsed = time.perf_counter() - t0
        # 按 cutoff 各算一组准确率；顶层 overall 取最大 cutoff（最完整上下文口径）
        cutoff_accuracy = {}
        for cutoff in CUTOFFS:
            acc = {}
            all_c = all_t = 0
            for cat in (1, 2, 3, 4):
                t = total[cutoff].get(cat, 0)
                cc = correct[cutoff].get(cat, 0)
                acc[_CAT_NAMES[cat]] = round(cc / t, 4) if t else 0.0
                all_c += cc
                all_t += t
            acc["overall"] = round(all_c / all_t, 4) if all_t else 0.0
            cutoff_accuracy[f"top_{cutoff}"] = {
                "accuracy": acc,
                "correct": {str(k): v for k, v in correct[cutoff].items()},
                "total": {str(k): v for k, v in total[cutoff].items()},
                "n_qa": all_t,
            }
        # 顶层 overall = 最大 cutoff 的（向后兼容：单 cutoff 时即唯一口径）
        _max_cut = max(CUTOFFS)
        _top = cutoff_accuracy[f"top_{_max_cut}"]
        accuracy = _top["accuracy"]
        all_total = _top["n_qa"]

        result = {
            "conversation_id": self.conv_id,
            "accuracy": accuracy,  # 顶层 = 最大 cutoff 口径（向后兼容）
            "correct": {str(k): v for k, v in _top["correct"].items()},
            "total": {str(k): v for k, v in _top["total"].items()},
            "n_qa": all_total,
            "skipped": skipped,  # 限流/瞬态失败未计分母的题数
            "cutoff_results": cutoff_accuracy,  # 各 cutoff 分桶（top_10/top_20/top_50/top_200）
            "elapsed_seconds": round(elapsed, 2),
        }
        with open(result_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
        # 日志打印各 cutoff 的 overall，一眼看 top_k 准确率曲线
        cutoff_summary = "  ".join(f"top_{c}={cutoff_accuracy[f'top_{c}']['accuracy']['overall']:.3f}"
                                    for c in CUTOFFS)
        logger.info("conv %d 完成: [%s] skipped=%d 耗时%.1fs",
                    self.conv_id, cutoff_summary, skipped, elapsed)
        return result


# =====================================================================
# TESTLOCOMO：数据加载 + 总体汇总 + metrics 输出
# =====================================================================
class TESTLOCOMO:
    @staticmethod
    def get_locomo_data(path: str):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def load_completed_convs(path: str) -> set[int]:
        """读已有 result，返回已完成（有 n_qa 计数）的 conversation_id 集合。"""
        if not os.path.exists(path):
            return set()
        completed = set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # conv 行有 conversation_id + n_qa；汇总行有 overall（顶层）跳过
                    if "conversation_id" in data and "n_qa" in data:
                        completed.add(int(data["conversation_id"]))
        except Exception as e:
            logger.warning("读已完成 conv 失败: %s", e)
        return completed

    @staticmethod
    def overall_compute(path: str) -> None:
        """跨 conv 汇总：按 cutoff 分桶各类准确率（微平均）+ overall + 限流 skip。

        兼容旧格式（无 cutoff_results）：按顶层 correct/total 单点聚合。
        """
        # 按 cutoff 聚合：agg[cutoff_label] = {"correct": {cat: n}, "total": {cat: n}}
        agg: dict[str, dict] = {}
        total_skipped = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 跳过汇总行：新格式 overall_by_cutoff，旧格式顶层 overall；均无 conversation_id
                if "conversation_id" not in data:
                    continue
                total_skipped += int(data.get("skipped", 0))
                cr = data.get("cutoff_results")
                if cr:
                    # 多 cutoff 格式：遍历每个 cutoff 分桶累加
                    for label, blk in cr.items():
                        agg.setdefault(label, {"correct": defaultdict(int),
                                                "total": defaultdict(int)})
                        for k, v in blk.get("correct", {}).items():
                            agg[label]["correct"][k] += v
                        for k, v in blk.get("total", {}).items():
                            agg[label]["total"][k] += v
                else:
                    # 旧格式（单点）：顶层 correct/total，归到一个 "overall" 桶
                    agg.setdefault("overall", {"correct": defaultdict(int),
                                                "total": defaultdict(int)})
                    for k, v in data.get("correct", {}).items():
                        agg["overall"]["correct"][k] += v
                    for k, v in data.get("total", {}).items():
                        agg["overall"]["total"][k] += v

        # 计算每个 cutoff 的各类准确率 + overall
        cutoff_accuracy = {}
        for label, blk in agg.items():
            acc = {}
            all_c = all_t = 0
            for c in (1, 2, 3, 4):
                t = blk["total"].get(str(c), 0)
                cc = blk["correct"].get(str(c), 0)
                acc[_CAT_NAMES[c]] = round(cc / t, 4) if t else 0.0
                all_c += cc
                all_t += t
            acc["overall"] = round(all_c / all_t, 4) if all_t else 0.0
            cutoff_accuracy[label] = {"accuracy": acc, "n_qa": all_t,
                                       "correct": dict(blk["correct"]),
                                       "total": dict(blk["total"])}

        logger.info("=" * 60)
        logger.info("跨 conv 汇总（按 cutoff 分桶，微平均）:")
        for label, blk in cutoff_accuracy.items():
            logger.info("  %s: overall=%.4f  %s",
                        label, blk["accuracy"]["overall"],
                        {k: blk["accuracy"][k] for k in _CAT_NAMES.values()})
        logger.info("限流 skip（未计分母）: %d 题", total_skipped)
        logger.info("=" * 60)
        with open(path, "a", encoding="utf-8") as f:
            # 单行 JSON（与 conv 行格式一致），保证 overall_compute 按行解析能正确跳过
            f.write(json.dumps({"overall_by_cutoff": cutoff_accuracy,
                                "total_skipped": total_skipped}, ensure_ascii=False) + "\n")

    @staticmethod
    def dump_metrics(metrics: dict, out_path: str) -> None:
        """输出各 conv 各阶段耗时 + token 消耗 + per-call 时延分位数(p50/p90/p95)。"""
        phase_meta = {
            "add_mem": "写入记忆(classifier 抽取)",
            "search": "检索记忆(纯向量召回,无LLM,token恒0)",
            "generate": "生成回答",
            "validate": "验证回答",
        }
        per_conv = {}
        agg = {p: [0, 0.0, 0, 0, 0] for p in phase_meta}
        for conv_id, blocks in sorted(metrics.items()):
            cs = {}
            for p in phase_meta:
                b = blocks.get(p, [0, 0.0, 0, 0, 0])
                cs[p] = {
                    "invoke_count": b[0],
                    "elapsed_seconds": round(b[1], 4),
                    "input_tokens": b[2],
                    "output_tokens": b[3],
                    "total_tokens": b[4],
                }
                for i in range(5):
                    agg[p][i] += b[i]
            per_conv[conv_id] = cs
        overall = {}
        latency_pct = {}  # 各阶段单次调用时延分位数（跨 conv 聚合，已升序）
        for p in phase_meta:
            a = agg[p]
            sorted_lat = sorted(phase_latencies.get(p, []))  # 从小到大排序
            overall[p] = {
                "invoke_count": a[0], "elapsed_seconds": round(a[1], 4),
                "input_tokens": a[2], "output_tokens": a[3], "total_tokens": a[4],
                # 分位数单位毫秒：单次调用耗时不直观，秒级 4 位小数读到 0.0023s 仍要
                # 心算。乘 1000 后 0.0023s → 2.3ms，p50/p90/p95 一眼可读。
                "p50_ms": round(_percentile(sorted_lat, 50) * 1000, 2),
                "p90_ms": round(_percentile(sorted_lat, 90) * 1000, 2),
                "p95_ms": round(_percentile(sorted_lat, 95) * 1000, 2),
            }
            latency_pct[p] = {
                "sample_count": len(sorted_lat),
                "min_ms": round(sorted_lat[0] * 1000, 2) if sorted_lat else 0.0,
                "p50_ms": round(_percentile(sorted_lat, 50) * 1000, 2),
                "p90_ms": round(_percentile(sorted_lat, 90) * 1000, 2),
                "p95_ms": round(_percentile(sorted_lat, 95) * 1000, 2),
                "max_ms": round(sorted_lat[-1] * 1000, 2) if sorted_lat else 0.0,
            }
        payload = {"phase_description": phase_meta,
                   "per_conversation": per_conv, "overall": overall,
                   "latency_percentiles": latency_pct}
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        for p, desc in phase_meta.items():
            o = overall[p]
            logger.info("[统计] %s(%s): 调用%d次 耗时%.2fs p50=%.1fms p90=%.1fms p95=%.1fms "
                        "input=%d output=%d total=%d",
                        desc, p, o["invoke_count"], o["elapsed_seconds"],
                        o["p50_ms"], o["p90_ms"], o["p95_ms"],
                        o["input_tokens"], o["output_tokens"], o["total_tokens"])
        logger.info("=" * 60)
        logger.info("单次调用时延分位数（已按从小到大排序后取分位，单位毫秒）:")
        for p, desc in phase_meta.items():
            lp = latency_pct[p]
            logger.info("  %s(%s): 样本%d min=%.1fms p50=%.1fms p90=%.1fms p95=%.1fms max=%.1fms",
                        desc, p, lp["sample_count"], lp["min_ms"], lp["p50_ms"],
                        lp["p90_ms"], lp["p95_ms"], lp["max_ms"])

    @staticmethod
    def dump_retrieval_subprocess_latencies(
        snapshot: dict, search_total_seconds: float, out_path: str
    ) -> None:
        """输出检索时延拆分：闭合阶段墙钟 + 并行段细项 + 闭合校验（单位毫秒）。

        三块结构：
        - closed_loop：retrieve 主路径 7 段首尾相接的串行墙钟，sum 线性可加——
          Σ sum_ms == search 阶段总耗时（误差<1ms，差值来自 api.recall 包装层 +
          未打点的纯内存赋值，见 closure_check.gap_ms）。分位数（p50 等）不可加，
          属百分位数数学性质，非闭合缺陷。
        - subprocess_detail：各后端单次调用原始样本，诊断「哪个后端慢」。不闭合——
          recall 段 6 路并行，子和 > 墙钟；redis_recheck 与 recheck_wall 交叉包含。
        - closure_check：search_total_ms（_PhaseTimer 记的 api.recall 总耗时）vs
          closed_loop_sum_ms（7 段 sum 之和），gap_ms 应<若干 ms，反映包装层开销。

        数据来自引擎内 LatencyCollector 单例（query_parser/milvus_vector/
        elasticsearch_fulltext/unit_reader/weighted_rrf_fuser/pipeline_retriever 打点）。
        """
        closed_desc = {
            "parse_wall": "查询理解整段(sanitize/分词/embedding/特征/时间解析)",
            "recall_wall": "谓词构造+多路并行召回整段墙钟(内部 milvus/ES 并行,子和>墙钟)",
            "fuse_wall": "RRF 融合+截断 budget 整段",
            "recheck_wall": "点读真源+后置过滤整段(含 redis_recheck 子项)",
            "rerank_wall": "可选精排整段(含 rerank 子项;rerank_enabled=false 时空转~0)",
            "threshold_wall": "阈值过滤+截断 top_k 整段",
            "disclose_wall": "渐进式披露整段(纯内容塑形,复用已点读 units)",
        }
        sub_desc = {
            "embedding": "query 向量化(embed_query, 远程 MaaS bge-m3)",
            "milvus_search": "Milvus ANN 向量检索(search, 远程 Milvus)",
            "milvus_get": "Milvus 按 id 点读(get, recall 内反查 metadata)",
            "es_search": "Elasticsearch BM25 关键词检索(search, 本地 ES)",
            "es_get": "Elasticsearch 按 id 点读(mget, 本地 ES)",
            "redis_recheck": "Redis 点读真源(recheck 段, UnitReader.load 内逐条 kv.get)",
            "rerank": "精排 rerank API 调用(远程 reranker, rerank_enabled=true 时生效)",
            "fuse": "RRF 多路融合排序(weighted_rrf, 纯内存计算)",
            "tokenize": "jieba 分词(query 主分词+特征抽取分词, parse 段内每次检索 2 次; 首样本含冷启动)",
        }

        def _stats(sorted_lat):
            if not sorted_lat:
                return {"sample_count": 0}
            total = sum(sorted_lat)
            return {
                "sample_count": len(sorted_lat),
                "min_ms": round(sorted_lat[0], 2),
                "p50_ms": round(_percentile(sorted_lat, 50), 2),
                "p90_ms": round(_percentile(sorted_lat, 90), 2),
                "p95_ms": round(_percentile(sorted_lat, 95), 2),
                "max_ms": round(sorted_lat[-1], 2),
                "sum_ms": round(total, 2),
                "mean_ms": round(total / len(sorted_lat), 2),
            }

        closed_loop = {k: _stats(snapshot.get(k, [])) for k in closed_desc}
        subprocess_detail = {k: _stats(snapshot.get(k, [])) for k in sub_desc}
        # 闭合校验：sum 线性可加，7 段 sum_ms 之和应 == search 阶段总耗时。
        closed_loop_sum_ms = sum(
            s.get("sum_ms", 0.0) for s in closed_loop.values()
        )
        search_total_ms = round(search_total_seconds * 1000.0, 2)
        gap_ms = round(search_total_ms - closed_loop_sum_ms, 2)
        closure_check = {
            "search_total_ms": search_total_ms,
            "closed_loop_sum_ms": round(closed_loop_sum_ms, 2),
            "gap_ms": gap_ms,
            "note": (
                "gap = search_total - closed_loop_sum，反映 api.recall 包装层 + 未打点"
                "纯内存赋值开销，应<若干 ms；subprocess_detail 因 recall 段并行，"
                "子和 > recall_wall 属正常（非闭合缺陷）。"
            ),
        }
        payload = {
            "phase_description": {**closed_desc, **sub_desc},
            "closed_loop": closed_loop,
            "subprocess_detail": subprocess_detail,
            "closure_check": closure_check,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        # 日志：闭合块（含闭合校验）+ 子项块
        logger.info("=" * 60)
        logger.info("检索时延闭合块（7 段墙钟，sum 线性可加 == search 总耗时）:")
        for slot, desc in closed_desc.items():
            st = closed_loop[slot]
            n = st.get("sample_count", 0)
            if n == 0:
                logger.info("  %s(%s): 无样本", desc, slot)
                continue
            logger.info("  %s(%s): 样本%d sum=%.1fms p50=%.1fms p90=%.1fms p95=%.1fms",
                        desc, slot, n, st["sum_ms"], st["p50_ms"], st["p90_ms"], st["p95_ms"])
        logger.info("  闭合校验: search_total=%.1fms closed_loop_sum=%.1fms gap=%.2fms",
                    closure_check["search_total_ms"],
                    closure_check["closed_loop_sum_ms"], gap_ms)
        logger.info("-" * 60)
        logger.info("检索子过程细项（各后端单次调用，不闭合，诊断用；recall 段子和>墙钟属正常）:")
        for slot, desc in sub_desc.items():
            st = subprocess_detail[slot]
            n = st.get("sample_count", 0)
            if n == 0:
                logger.info("  %s(%s): 无样本", desc, slot)
                continue
            logger.info("  %s(%s): 样本%d sum=%.1fms p50=%.1fms p90=%.1fms p95=%.1fms max=%.1fms",
                        desc, slot, n, st["sum_ms"], st["p50_ms"], st["p90_ms"],
                        st["p95_ms"], st["max_ms"])


def main() -> int:
    logger.info("=" * 60)
    logger.info("LOCOMO v5 评测（mem2.0 架构, 精简冒烟, 单判, 10并发, 真后端 Redis+Milvus+ES）")
    logger.info("=" * 60)

    # 1) 装配（主线程一次，worker 共享 + scope 隔离）
    config = _build_config()
    logger.info("[assemble] 装配 MemoryAPI ...")
    t0 = time.perf_counter()
    api = assemble(config=config)
    logger.info("[assemble] 完成 %.2fs api=%s", time.perf_counter() - t0, type(api).__name__)

    # 1.5) 构造共享 LLM 实例（generate/judge 用，与引擎内 LLM 同配置；
    #       OpenAILLM.chat 已被 patch，token 自动归类到 generate/validate phase）
    llm = OpenAILLM(
        model_name=os.getenv("MODEL_NAME", ""),
        base_url=os.getenv("API_BASE", ""),
        api_key=os.getenv("API_KEY", ""),
        default_temperature=float(os.getenv("MODEL_TEMPERATURE", "0.01")),
        default_max_tokens=4096,
    )
    logger.info("[llm] 共享 OpenAILLM 实例: model=%s base=%s",
                getattr(llm, "_fallback_model", ""), getattr(llm, "_fallback_base_url", ""))

    # 2) 加载数据
    data = TESTLOCOMO.get_locomo_data(data_path)
    total_convs = len(data)
    logger.info("[data] %d conversations from %s", total_convs, data_path)

    # 3) 选 conv
    _conv_ids_env = os.getenv("CONV_IDS", "").strip()
    if _conv_ids_env:
        _selected = {int(x.strip()) for x in _conv_ids_env.split(",") if x.strip().isdigit()}
        conv_ids = [c for c in range(total_convs) if c in _selected]
        logger.info("CONV_IDS 指定只跑 conv %s", sorted(_selected))
    else:
        conv_ids = [c for c in range(total_convs) if c >= START_CONV]

    # 4) 断点恢复
    if RESUME:
        completed = TESTLOCOMO.load_completed_convs(result_path)
        logger.info("RESUME: 跳过 %d 个已完成 conv %s", len(completed), sorted(completed))
    else:
        open(result_path, "w", encoding="utf-8").close()  # 清空
        completed = set()
    todo = [c for c in conv_ids if c not in completed]
    logger.info("[run] 待跑 conv %s (共 %d), 并发度=%d", todo, len(todo), CONCURRENCY)

    # 4.5) 落本轮元信息：事后看一个 runs 目录时，光有结果无法判断它是哪组参数跑出来的。
    _dump_run_meta(todo, data_path)

    # 5) 10 并发执行
    failures = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="v5") as ex:
        future_map = {
            ex.submit(ConversationProcessor(c, api, llm).process_full, data[c]): c
            for c in todo
        }
        for fut in as_completed(future_map):
            cid = future_map[fut]
            try:
                fut.result()
            except Exception as e:
                failures.append((cid, str(e)))
                logger.error("conv %d 未捕获异常: %s", cid, e)

    # 6) 汇总
    if failures:
        logger.warning("失败 conv: %s", [(c, e[:80]) for c, e in failures])
    TESTLOCOMO.overall_compute(result_path)
    TESTLOCOMO.dump_metrics(phase_metrics, metrics_path)
    # 检索时延拆分已移除：引擎已重构掉 LatencyCollector 打点，模块不存在，
    # retrieval_subprocess_latency_v5*.json 不再生成（引擎侧无打点数据可取）。
    logger.info("结果: %s", result_path)
    logger.info("指标: %s", metrics_path)
    _finalize_run_dir()
    logger.info("完成!")
    return 0 if not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        # 线程池模式下各线程独立 asyncio.run，无需 WindowsSelectorEventLoopPolicy；
        # 但保留以防 worker 内引擎协程偏好 selector。
        pass
    sys.exit(main())
