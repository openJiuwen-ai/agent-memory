# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentLlmExtractor — llm extractor 的 agent 方向版（F09）。

同一套抽取管线与输出契约（继承 :class:`ExtractorImpl`），分化点是 prompt 指令面的三点：

- **场景重定向**：``llm`` 的 prompt 是人际对话方向（A/B/C 对称多方、无提取中心）；
  本版面向 user↔agent 对话——以用户为中心提取（用户的事实/偏好/决定/计划/身份/处境），
  助手的输出（解释/建议/代码/总结）本身不是条目，用户对其行动时才成条、记用户的决定。
- **语言可配**：``extractor_language``（默认 ``cn``）切换指令面语言。输出契约不随语言
  切换——source_id / tier / memory_class 枚举值 / JSON schema 骨架恒英文，解析端
  ``build_candidates`` 零改动；条目 content 的语言仍跟源文本（「绝不翻译」规则两版都保留）。
- **抽取时同步判定 memory_class**（三类归属类别，见 ``_MEMORY_CLASSES``）：与
  Router 判定的差别在于省一次独立 LLM 调用——判定输入（content）本就是抽取产物。
  值经基类 ``build_candidates`` 透传进 ``system_metadata[MEMORY_CLASS_KEY]``，本类
  ``extract`` 收口值域（非法/缺失兜底 ``long_term_memory``）。该键同时是文档模式
  md 落盘的分流键（F08 §3 ``_PATH_MAP`` 已加同名映射，三类各归其位）。

prompt 面经 F09 §2.1 类属性化挂在 ``ExtractorImpl`` 类属性上，本子类构造时按
language 选双语常量对赋实例属性，抽取管线零改动。
装配：``extractor.target: agent_llm`` + ``params.extractor_language: cn|en``。
"""

from __future__ import annotations

from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MEMORY_CLASS_KEY, MemoryUnit

from ..base import ExtractContext
from ..extractor import ExtractorProducer
from .llm_extractor import _DEFAULT_EXTRACT_BATCH_SIZE, ExtractorImpl

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 主提取 prompt —— user↔agent 场景重定向版（F09 §4）。抽取纪律继承 llm 版并精简
# 为十条（忠实/具体/自包含合并为一条、社交外壳与不记录合并为一条），新增
# memory_class 三类判定（语义对齐 Router 判定但省一次独立 LLM 调用）。两版均为
# 新写，EN 版不复用 llm 的 _EXTRACT_SYSTEM_PROMPT。
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT_CN = """\
只输出一个 JSON 数组。不要解释，不要 markdown 代码块。

这是一场用户与助手的对话。对每一行源文本：判断是哪一方在说话，以该说话人解析代词，\
以观测日期为基准解析时间，判断该行做了什么，然后提取条目。

规则：

- 提取该行陈述的事实，以及由它一步直接推出的内容。绝不臆测。一切保持具体：具名\
实体、数量与描述性属性以源文本自己的措辞保留，绝不用它所属的类别替代；每条自包含，\
携带赋予该事实意义的时间、地点、当事方或场合。
  好 "用户飞往里斯本的航班延误了两小时。"  坏 "用户的出行受到了影响。"

- 记录以用户为中心：条目记录用户的事实、偏好、决定、计划、身份与处境，以及用户\
谈到的关于世界与他人的事。助手自己的输出——解释、建议、代码、总结——本身不是\
条目；仅当用户对它有所行动时（"就按你说的办""选第二个方案吧"）才成为条目，且\
条目记录的是用户的决定，指明决定了什么。

- 粒度跟随信息而非长度：一件事一条，且该行提出的每一件事都要成为一条。一个主张\
连同为它给出的理由或属性是同一件事——不拆成并列碎片，不让第二件事在合并中消失。\
绝不泛化、拔高抽象层级或跨源合并。

- 对先前立场的否定、撤回或修正是独立的事，与它所否定的主张、以及取代它的任何新\
偏好分开。"我之前说错了；我不用 macOS，我用 Windows" 是三条：用户撤回先前使用\
macOS 的说法（episodic）、用户不使用 macOS（semantic）、用户使用 Windows\
（semantic）。否定要写成先前陈述的直接反面——埋在肯定表述里的否定无法与它要\
覆盖的记录匹配，过期记忆就永远不会被替代。

- 写出该行「做了什么」。若命题不论这句话是否被说出都成立，该动词是转述壳（说、\
表示、提到）——去掉它，直接陈述事实。若话语行为本身造就事实——请求、批准、\
拒绝、承诺或回答——保留该动词并指名它针对谁。
  好 "2026-03-14，被问到服务部署在哪个区域时，用户选择了杭州区域——须控制在\
大陆预算内。"
  坏 "用户提到截止日期是周五。"  →  "截止日期是周五。"

- 说话人归属：用户行里的"我/我们"是用户、"你/你们"是助手；助手行里相反。每条以\
第三人称书写并带显式主语（"用户……""助手……"或人名），按源文本的称呼方式命名\
各方，解析所有代词。绝不把助手只是提议、请求或假设的内容断言为用户的事实：助手行\
"我已续期了证书，你也该轮换你的" 产出 "助手续期了证书" 与 "助手建议用户轮换用户\
的证书"——绝不是 "用户轮换了证书"。用户谈到的人是第三方：按用户的陈述保留他们\
的名字与事实。

- 时间。用户消息以 "observation_date: YYYY-MM-DD" 开头——这是各行被说出的日期，\
也是一切时间表达的解析基准。
  (a) 将条目定位于时间的信息必须写进 CONTENT 文本——仅存于 "event_date" 的时间\
不可见、不生效。除观测日期外源文本未给出其他时间时，观测日期即该时间。不依赖任何\
时间即成立的事实无需时间。
  (b) 将一切相对表达（"昨天""一年前"）对照该日期解析为绝对时间并去掉原措辞——\
绝不两份并存。
  (c) 绝不声称源文本没有的精度：说了具体某天就保持某天，"上个月" 归为月份，\
"上周" 归为区间；不发明小时。
  (d) "event_date"：ISO 8601，条目无时间成分时省略。月份或区间精度时，把首日\
写在这里，content 文本保持较粗精度。

- 语言：每条陈述用与其源相同的语言书写。绝不翻译。

- 回应或承接上一行的发言，自身可能是省略句。从近期上下文将其补全为独立条目；条目\
仍属于该行，保留其 source_id。
  好的上下文 "助手提议用 Rust 重写解析器，并问从哪里开始"
       该行 "[user]: 先做词法分析器。"  →  "用户计划从词法分析器开始这次重写。"

- 不记录只有本次交流才需要的东西：社交外壳（问候、致谢、赞美、道别）、纯本地指称\
（"那段代码""如上所述"）、助手未经用户确认的建议、无长期价值的过程细节。社交\
外壳也不会让该行的其余部分消失——剥去外壳后判断该行增添了什么；仅当什么都不剩时\
才返回 []。拿不准时，只记录在将来与该用户的对话中仍然要紧的东西。

字段：
- "source_id"："[ID: ...]" 标记中的裸 UUID，不带外壳、不带引号——即该条所述的行。\
上下文可提供指称对象或省略句所回应的内容，但绝不改变此 id；仅由上下文提出的\
事不是条目。
- "memory_class"：条目的归属类别，三选一：
  "user_profile"——用户的稳定画像：身份、偏好、习惯、能力、长期处境\
（"用户偏好书面交接而非口头交接"）。
  "long_term_memory"——跨会话有长期价值的事实、决定与知识：项目与技术决策、\
用户谈到的关于世界与他人之事（"用户的项目选了杭州区域"）。
  "daily_memory"——短期时效内容：近期安排、待办、当日计划与过程（"用户计划周五\
晚聚餐"）。
  判定顺序：关于用户本人的稳定属性归 "user_profile"；有明确短期时效、过期即失效\
的归 "daily_memory"；其余归 "long_term_memory"。拿不准时用 "long_term_memory"。
- "confidence"：直接陈述 1.0，直接蕴含 0.7，弱暗示 0.5。绝不低于 0.5。
- "tier"："episodic" 发生在某个时间点；"semantic" 是事实、概念或偏好；\
  "procedural" 是某事的做法。拿不准时用 "semantic"。
- "title"：为该条所记「这件事」命名的短标题（如 "服务区域选择" / "证书已轮换"），\
  用内容自身的语言，20 字以内。命名具体的事，绝不命名类别——"事实" 或 "偏好"\
  是错的。它不是 tag：每条恰一个标题，且可与所有 tag 不同。本批各条所述之事\
  不同时，避免跨条重复同一标题。
- "tags"：1-3 个简短的小写主题标签，用内容自身的语言，不复述 content 中已居\
  核心地位的词。

输出 schema：
[{
  "source_id": "32049cd0-5f7c-419f-928f-503b24318f7c",
  "target": "fact" | "event" | "preference" | "context" | "structured_record" | "artifact",
  "memory_class": "user_profile" | "long_term_memory" | "daily_memory",
  "tier": "episodic" | "semantic" | "procedural",
  "content": "一条自包含的陈述，一切具体细节保留",
  "title": "命名这件事的短标题",
  "tags": ["tag1", "tag2"],
  "confidence": 0.5~1.0,
  "event_date": "2026-03-14"
}]
"""

_EXTRACT_SYSTEM_PROMPT_EN = """\
Output ONLY a JSON array. No explanation, no markdown fences.

This is a conversation between a user and the assistant. For each source line: identify
which side is speaking, resolve pronouns against that speaker, resolve time against the
observation date, decide what the line does, then extract items.

Rules:

- Extract what the line states, plus what follows from it in one direct step. Never
speculate. Keep every specific: named entities, quantities and descriptive attributes
survive in the source's own wording, never replaced by their category; each item is
self-contained, carrying whatever time, place, party or occasion gives the fact its
meaning.
  GOOD "The user's flight to Lisbon was delayed by two hours."  BAD "The user's travel
  was disrupted."

- The record is user-centred: items record the user's facts, preferences, decisions,
plans, identity and circumstances, and the matters the user raises about the world and
about other people. The assistant's own output - explanations, suggestions, code,
summaries - is not itself an item; it becomes one only where the user acts on it
("do it your way", "let's go with option two"), and then the item records the USER's
decision, naming what was decided.

- Granularity follows the information, not the length: one matter per item, and every
matter the line raises becomes an item. A claim plus the reasons or attributes offered
for it is ONE matter - do not split into parallel fragments, do not let a second matter
vanish into the first. Never generalise, raise abstraction, or combine across source
lines.

- Negation, retraction or correction of an earlier position is its own matter, separate
from the claim it cancels and from any new preference that replaces it. "I was wrong
before; I don't use macOS, I use Windows" is three items: the user retracts the earlier
claim of using macOS (episodic), the user does not use macOS (semantic), the user uses
Windows (semantic). Word the negation as the direct opposite of the earlier statement -
a negation buried in a positive claim cannot be matched against the record it overrides,
so the stale memory is never superseded.

- Write what the line DOES. If the proposition holds whether or not it was uttered, the
verb is a reporting wrapper (said, stated, mentioned) - drop it and state the fact
directly. If the utterance itself creates the fact - request, approval, refusal, promise
or answer - keep that verb and name whom it addresses.
  GOOD "On 2026-03-14, asked where to host the service, the user chose the Hangzhou
        region - it must stay within the mainland budget."
  BAD  "The user mentioned that the deadline is Friday."  ->  "The deadline is Friday."

- Speaker attribution: in a user line "I/me/my/we" is the user and "you/your" is the
assistant; in an assistant line the reverse. Write every item in the third person with
an explicit subject ("the user ...", "the assistant ..." or a person's name), naming
each party as the source labels them, and resolve every pronoun. Never assert of the
user what the assistant only proposed, asked or assumed: an assistant line "I've renewed
the certificate, and you should rotate yours" yields "The assistant renewed the
certificate" and "The assistant advised the user to rotate the user's certificate" -
never "The user rotated the certificate". People the user talks about are third
parties: keep their names and facts as the user states them.

- Time. The user message opens with "observation_date: YYYY-MM-DD" - the date the lines
were said and the reference for every time expression.
  (a) Whatever places the item in time belongs in the CONTENT TEXT - a time left in
"event_date" alone is invisible and does not count. The observation date is that time
unless the source gives another. A fact that holds without reference to any time needs
none.
  (b) Resolve every relative expression ("yesterday", "a year ago") against that date
and REMOVE the original wording - never keep both.
  (c) Never state a precision the source lacks: a named day stays a day, "last month"
becomes a month, "last week" a range; do not invent an hour.
  (d) "event_date": ISO 8601, omitted when the item has no time component. At month or
range precision, put the first day here and keep the content text coarser.

- Language: write each statement in the same language as its source. Never translate.

- A line that answers or continues the one before it may be elliptical on its own.
Complete it from the recent context into a standalone item; the item is still this
line's and keeps its source_id.
  GOOD context "The assistant proposes rewriting the parser in Rust and asks where to
       start" line "[user]: The lexer first."  ->  "The user plans to start the rewrite
       with the lexer."

- Do not record what only this exchange needs: social framing (greeting, thanks,
praise, sign-off), purely local references ("that code", "as above"), the assistant's
unconfirmed suggestions, and process detail with no lasting value. Social framing also
does not make the rest of the line disappear - judge what the line adds once it is set
aside; return [] only when nothing is left. When in doubt, record only what would still
matter in a future conversation with this user.

Fields:
- "source_id": the bare UUID from the "[ID: ...]" marker, no wrapper or quotes - the
line the item is about. Context may supply a referent or what an elliptical line
answers, and never changes this id; a matter only the context raises is not an item.
- "memory_class": the ownership class of the item, one of three:
  "user_profile" - the user's stable profile: identity, preferences, habits, skills,
long-term circumstances ("The user prefers written handovers over verbal ones").
  "long_term_memory" - facts, decisions and knowledge with lasting value across
sessions: project and technical decisions, matters the user raises about the world and
other people ("The user's project chose the Hangzhou region").
  "daily_memory" - short-lived content: near-term arrangements, to-dos, same-day plans
and process ("The user plans a dinner for Friday night").
  Order of decision: a stable attribute of the user's own is "user_profile"; anything
with a clear short-term horizon that lapses once passed is "daily_memory"; the rest is
"long_term_memory". When unsure, "long_term_memory".
- "confidence": 1.0 stated outright, 0.7 directly entailed, 0.5 weakly suggested. Never
emit below 0.5.
- "tier": "episodic" happened at a point in time; "semantic" a fact, concept or
preference; "procedural" how something is done. When unsure, "semantic".
- "title": a short heading naming THE MATTER this item records (e.g. "Service region
choice" / "Certificate rotated"), in the content's language, about 20 characters or
fewer. It names the specific matter, never a category - "Fact" or "Preference" is
wrong. It is not a tag: exactly one title per item, and it may differ from every tag.
Avoid repeating the same title across items of this batch when the matters differ.
- "tags": 1-3 short lowercase labels for the topic, in the content's language, not
repeating words already central to the content.

Output schema:
[{
  "source_id": "32049cd0-5f7c-419f-928f-503b24318f7c",
  "target": "fact" | "event" | "preference" | "context" | "structured_record" | "artifact",
  "memory_class": "user_profile" | "long_term_memory" | "daily_memory",
  "tier": "episodic" | "semantic" | "procedural",
  "content": "one self-contained statement, every specific kept",
  "title": "short heading naming the matter",
  "tags": ["tag1", "tag2"],
  "confidence": 0.5~1.0,
  "event_date": "2026-03-14"
}]
"""

# ---------------------------------------------------------------------------
# 过程记忆汇总 prompt —— user-agent 方向（原文已是 user 视角，EN 版沿用其纪律，
# CN 版同批翻译）。procedural 路径不输出 memory_class（extract 兜底默认类）。
# ---------------------------------------------------------------------------

_PROCEDURAL_SYSTEM_PROMPT_CN = """\
把下面的对话汇总成一条结构化的执行历史记忆。

只输出一个 JSON 对象（不要数组，不要 markdown 代码块）。Schema：
{
  "content": "一条自包含的陈述，概括本轮做了什么：目标/任务、关键步骤、结果。\
结构化但作为单条连贯陈述；Goal:/Steps:/Result: 之类的内联标记可选，使用时须\
与源文本的语言一致。整条陈述用与源文本相同的语言书写。覆盖整轮，不只一条事实。",
  "title": "为本轮完成的事命名的短标题，用源文本的语言，约 20 字以内。\
命名具体的事，绝不命名类别。"
}

规则：
- 恰好产出一条记忆（整轮作为一条过程记录）。
- 不拆成多条事实；不输出 JSON 数组。
- 捕捉过程/做法：用户想要什么、做了什么、如何结束。
- 保持自包含（脱离源对话可理解）。
- 若本轮无可行的过程（如纯问候），仍以一条陈述概括其意图与结果。
"""

_PROCEDURAL_SYSTEM_PROMPT_EN = """\
Summarize the conversation below into ONE structured execution-history memory.

Output ONLY a JSON object (no array, no markdown fences). Schema:
{
  "content": "one self-contained statement summarizing what was done in this turn: \
the goal/task, the key steps taken, and the outcome/result. Structured but as a single \
coherent statement; inline markers such as Goal:/Steps:/Result: are optional, and must be \
written in the source's language when used. Write the whole statement in the SAME \
language as the source text. Cover the whole turn, not just one fact.",
  "title": "a short heading naming what this turn accomplished, in the source's \
language, about 20 characters or fewer. Names the specific matter, never a category."
}

Rules:
- Produce exactly ONE memory (the whole turn as one procedural record).
- Do NOT fragment into multiple facts; do NOT output a JSON array.
- Capture the procedure/how-to: what the user wanted, what was done, how it ended.
- Keep it self-contained (understandable without the source conversation).
- If the turn has no actionable procedure (e.g. pure greeting), still summarize its intent
  and outcome in one statement.
"""

# ---------------------------------------------------------------------------
# 评测保真追加段（LongMemEval opt-in；场景中立纯纪律，CN 版为同批翻译）。
# ---------------------------------------------------------------------------

_EXTRACTION_FIDELITY_SUFFIX_CN = """\
仅本次写入生效的附加保真契约：

- 不要把不同源文本行的独立事项合并成一条。
- 普通回答或确认只产出它贡献的已决命题；不要再额外产出 "回答了……" 式的重复包装。
- 补全信息性的当前省略行时，只从最直接相关的上下文复制最小缺失的主语、谓语、宾语、
  单位或时间。绝不产出只属于上下文的事实。
- 返回前自查：每条当前源文本行贡献的每个持久实体、数值、日期、否定、更新与选择都
  恰好出现一次。
"""

_EXTRACTION_FIDELITY_SUFFIX_EN = """\
Additional fidelity contract for this write only:

- Do not merge independent matters from different source lines into one item.
- A normal answer or confirmation becomes the resolved proposition it contributes; do
  not also emit a duplicate "answered that ..." wrapper.
- Complete an informative CURRENT elliptical line from only the immediately relevant
  context, copying the minimum missing subject, predicate, object, unit or time. Never
  emit a fact that belongs only to context.
- Before returning, verify that every durable entity, value, date, negation, update and
  choice contributed by each current source line appears exactly once.
"""

# ---------------------------------------------------------------------------
# user prompt 参考段模板 —— 与 llm_extractor._CONTEXT_SECTION_TEMPLATES 同构
# （键名一致，条目行恒 "- {content}"），标题与行为指令双语。
# ---------------------------------------------------------------------------

_CONTEXT_SECTION_TEMPLATES_CN = {
    "recent_heading": (
        "## 近期对话上下文（仅用于指代/代词消解——不要从中提取条目，"
        "不要把 source_id 指向这些行）：\n"
    ),
    "related_heading": (
        "## 已有相关记忆（已知事实——不要重复产出其已涵盖的信息。\n"
        "但与已有记忆相矛盾、更正或修订的行不算重复：照常提取为新条目，"
        "让冲突在下游消解。不要仅因主题与已有记忆相同就返回 []。）\n"
    ),
}

_CONTEXT_SECTION_TEMPLATES_EN = {
    "recent_heading": (
        "## Recent conversation context "
        "(for coreference / pronoun resolution only — do NOT extract from these, "
        "do NOT point source_id at these):\n"
    ),
    "related_heading": (
        "## Existing related memories "
        "(already-known facts — do NOT duplicate information these cover.\n"
        "However, a line that CONTRADICTS, CORRECTS or REVISES an existing "
        "memory is NOT a duplicate: extract it as a new item so the conflict "
        "can be resolved downstream. Do NOT return [] merely because the topic "
        "matches an existing memory.)\n"
    ),
}

# ---------------------------------------------------------------------------
# observation_date 前缀模板 —— 与 llm_extractor._OBSERVATION_DATE_PREFIX 同构
# （"{date}" 占位符；行键 "observation_date: " 恒英文——system prompt 引用该行格式）。
# ---------------------------------------------------------------------------

_OBSERVATION_DATE_PREFIX_CN = (
    "观测日期: {date}\n"
    "（以该日期为基准，把 \"昨天\"/\"上周\"/\"明天\" 之类的相对时间表达"
    "解析为 content 与 event_date 中的绝对日期。）\n\n"
)

_OBSERVATION_DATE_PREFIX_EN = (
    "observation_date: {date}\n"
    "(Use this as the reference point to resolve relative time expressions "
    "like \"yesterday\"/\"last week\"/\"tomorrow\" into absolute dates in content "
    "and event_date.)\n\n"
)

# memory_class 三类（F09）：agent 场景的归属类别，文档模式 md 落盘分流键的取值域。
# 语义与 Router 判定的 user_memory/project_memory/team_memory 三类一一对应、共享
# 同一路径（F08 §3 _PATH_MAP 加同名映射）。默认 long_term_memory——三类里最宽的
# 桶，拿不准时宁可长期保留，不按天丢弃。
_MEMORY_CLASSES = frozenset({"user_profile", "long_term_memory", "daily_memory"})
_DEFAULT_MEMORY_CLASS = "long_term_memory"


class AgentLlmExtractor(ExtractorImpl):
    """llm 的 agent 方向版：管线与输出契约继承，prompt 指令面按场景（user↔agent）
    重定向、按 language 切换；抽取时同步判定 memory_class（三类归属类别，替代
    Router 判定的一次独立 LLM 调用，F09）。"""

    def __init__(self, language: str = "cn", **kwargs: object) -> None:
        super().__init__(**kwargs)
        lang = language if language in {"cn", "en"} else "cn"
        if lang == "cn":
            self.EXTRACT_SYSTEM_PROMPT = _EXTRACT_SYSTEM_PROMPT_CN
            self.PROCEDURAL_SYSTEM_PROMPT = _PROCEDURAL_SYSTEM_PROMPT_CN
            self.EXTRACTION_FIDELITY_SUFFIX = _EXTRACTION_FIDELITY_SUFFIX_CN
            self.OBSERVATION_DATE_PREFIX = _OBSERVATION_DATE_PREFIX_CN
            self.CONTEXT_SECTION_TEMPLATES = _CONTEXT_SECTION_TEMPLATES_CN
        else:
            self.EXTRACT_SYSTEM_PROMPT = _EXTRACT_SYSTEM_PROMPT_EN
            self.PROCEDURAL_SYSTEM_PROMPT = _PROCEDURAL_SYSTEM_PROMPT_EN
            self.EXTRACTION_FIDELITY_SUFFIX = _EXTRACTION_FIDELITY_SUFFIX_EN
            self.OBSERVATION_DATE_PREFIX = _OBSERVATION_DATE_PREFIX_EN
            self.CONTEXT_SECTION_TEMPLATES = _CONTEXT_SECTION_TEMPLATES_EN

    def extract(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[MemoryUnit]:
        """抽取 + memory_class 值域收口。

        LLM 输出的 memory_class 经基类 ``build_candidates`` 透传进派生 unit 的
        ``system_metadata[MEMORY_CLASS_KEY]``；本方法对返回值做值域校验：合法三类
        保留，非法（warning 留痕）或缺失兜底 ``long_term_memory``。procedural 汇总
        路径的 prompt 未要求输出该字段，同样落默认类。
        """
        derived = super().extract(units, context=context)
        for unit in derived:
            metadata = unit.system_metadata or {}
            memory_class = str(metadata.get(MEMORY_CLASS_KEY, "") or "").strip()
            if memory_class in _MEMORY_CLASSES:
                continue
            if memory_class:
                logger.warning(
                    "AgentLlmExtractor: 非法 memory_class=%r，兜底 %s（unit=%s）",
                    memory_class,
                    _DEFAULT_MEMORY_CLASS,
                    unit.id[:8],
                )
            metadata[MEMORY_CLASS_KEY] = _DEFAULT_MEMORY_CLASS
            unit.system_metadata = metadata
        return derived


# -- 注册到 ExtractorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@ExtractorProducer.register("agent_llm")
def _build(config):
    return AgentLlmExtractor(
        llm=LlmProducer.dep(config, default="echo"),
        min_confidence=config.get("extractor_min_confidence", 0.5),
        max_attempts=config.get("extractor_retry_max", 3),
        retry_backoff_ms=config.get("extractor_retry_backoff", 1000),
        extract_batch_size=config.get("extract_batch_size", _DEFAULT_EXTRACT_BATCH_SIZE),
        language=config.get("extractor_language", "cn"),
    )
