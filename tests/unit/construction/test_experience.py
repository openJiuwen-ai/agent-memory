"""经验记忆共用部件（experience.py）的单测，按源文件的五节组织。

记录（键构造与三个 Store）、报错规则、轨迹消息、密钥清洗与脱敏检查、作者
摘要。评价与抽取两个算子自身的测试分别在 test_experience_appraiser.py 与
test_experience_extractor.py。
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.experience import UtilityStore, now_iso
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.raw import trajectory_key
from jiuwen_memory.construction.experience import (
    REPRO_UNIT_TRAJECTORY,
    REPRO_UNIT_USER,
    TRAJECTORY_TTL_SECONDS,
    CuratedSpaceConfig,
    ExecutionTrajectoryRecord,
    ExperienceConfig,
    TrajectoryStore,
    VerdictRecord,
    VerdictStore,
    author_digest,
    author_principal,
    compress_trace,
    desensitization_hit,
    first_user_message,
    has_error_mark,
    has_unresolved_error,
    is_role,
    last_assistant_reply,
    last_post_result_user_message,
    last_user_message,
    make_erasure_callback,
    scrub_messages,
    scrub_text,
    subject_identifiers,
    trajectory_expires_at,
    unresolved_tool_errors,
    verdict_key,
)
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

# ====================================================================== #
# 记录：键构造与三个 Store
# ====================================================================== #
# 事件键含判定修订号、只 insert 的幂等语义（断点续做不重复计效用）与复现作者
# 至多 2 个的上限，都是周期整理与检索放行条件的直接输入——错了会让同一轨迹的
# 效用翻倍计入或放行条件提前满足。

AUTHOR = "d" * 64  # 作者摘要（hex）
SCOPE = Scope(org="acme", user="u1")


class TestKeyBuilders:
    @staticmethod
    def test_trajectory_key_layout():
        assert trajectory_key(AUTHOR, "t1") == f"/trajectories/{AUTHOR}/t1", (
            "轨迹键：/trajectories/{作者摘要}/{trajectory_id}"
        )

    @staticmethod
    def test_verdict_key_layout():
        assert verdict_key(AUTHOR, "t1") == f"/verdicts/{AUTHOR}/t1", "判定键前缀与摘要"



class TestTrajectoryStore:
    @staticmethod
    def test_save_then_load_roundtrip():
        kv = InMemoryKVStore()
        store = TrajectoryStore(kv)
        record = ExecutionTrajectoryRecord(
            trajectory_id="t1",
            user=Scope(org="acme", user="u1", session="s1"),
            producer=Scope(org="acme", agent="agent-core"),
            credential_id="cred-1",
            messages=[{"role": "user", "content": "hi"}],
            capabilities=[{"kind": "tool", "name": "bash", "description": "run"}],
            retrieved=[{"id": "e1", "ref": "v1"}],
            tool_call_count=2,
            external_score=0.8,
            context={"turn": 3, "end_state": "completed", "extra": {"os": "linux"}},
            status="processing",
            started_at=now_iso(),
            expires_at=trajectory_expires_at(),
        )
        store.save(SCOPE, AUTHOR, record)
        loaded = store.load(SCOPE, AUTHOR, "t1")
        assert loaded is not None, "保存后应能读回"
        assert loaded.trajectory_id == "t1", "trajectory_id 往返一致"
        assert loaded.user == Scope(org="acme", user="u1", session="s1"), (
            "归属主体四维往返一致（space 不入归属）"
        )
        assert loaded.producer == Scope(org="acme", agent="agent-core"), (
            "生产者往返一致"
        )
        assert loaded.credential_id == "cred-1", "凭据指纹往返一致"
        assert loaded.producer_principal == "agent:agent-core", "生产者主体推导"
        assert loaded.messages == [{"role": "user", "content": "hi"}], "messages 往返一致"
        assert loaded.capabilities[0]["name"] == "bash", "capabilities 往返一致"
        assert loaded.retrieved == [{"id": "e1", "ref": "v1"}], "retrieved 往返一致"
        assert loaded.tool_call_count == 2, "tool_call_count 往返一致"
        assert loaded.external_score == pytest.approx(0.8), "external_score 往返一致"
        assert loaded.context == {"turn": 3, "end_state": "completed", "extra": {"os": "linux"}}, (
            "背景信息往返一致"
        )
        assert loaded.status == "processing", "status 往返一致"

    @staticmethod
    def test_load_missing_returns_none():
        store = TrajectoryStore(InMemoryKVStore())
        assert store.load(SCOPE, AUTHOR, "missing") is None, "不存在返回 None"

    @staticmethod
    def test_save_overwrites_status():
        kv = InMemoryKVStore()
        store = TrajectoryStore(kv)
        record = ExecutionTrajectoryRecord(
            trajectory_id="t1", expires_at=trajectory_expires_at()
        )
        store.save(SCOPE, AUTHOR, record)
        record.status = "done"
        store.save(SCOPE, AUTHOR, record)
        loaded = store.load(SCOPE, AUTHOR, "t1")
        assert loaded is not None and loaded.status == "done", "覆写保存应更新状态"

    @staticmethod
    def test_ttl_follows_expires_at():
        """TTL 按 expires_at 剩余时长：过期后惰性清除读不回。"""
        kv = InMemoryKVStore()
        store = TrajectoryStore(kv)
        record = ExecutionTrajectoryRecord(
            trajectory_id="t1",
            expires_at=(datetime.now(UTC) + timedelta(milliseconds=80)).isoformat(),
        )
        store.save(SCOPE, AUTHOR, record)
        assert store.load(SCOPE, AUTHOR, "t1") is not None, "未过期应读回"
        import time

        time.sleep(0.25)
        assert store.load(SCOPE, AUTHOR, "t1") is None, "过期后应读不回（TTL 生效）"

    @staticmethod
    def test_scope_isolated():
        kv = InMemoryKVStore()
        store = TrajectoryStore(kv)
        other = Scope(org="acme", user="u2")
        record = ExecutionTrajectoryRecord(
            trajectory_id="t1", expires_at=trajectory_expires_at()
        )
        store.save(SCOPE, AUTHOR, record)
        assert store.load(other, AUTHOR, "t1") is None, "写入落点按 scope 隔离"


class TestVerdictStore:
    @staticmethod
    def test_save_then_load_roundtrip():
        store = VerdictStore(InMemoryKVStore())
        record = VerdictRecord(
            outcome="success",
            evidence="user_feedback",
            revision=2,
            judge_version="3",
            model="gpt-x",
            judged_by="external",
            evidence_msgs=[1, 3],
            derived_ids=["e1", "e2"],
            coverage_gap=True,
            judged_at=now_iso(),
        )
        store.save(SCOPE, AUTHOR, "t1", record)
        loaded = store.load(SCOPE, AUTHOR, "t1")
        assert loaded is not None, "保存后应能读回"
        assert loaded.outcome == "success", "outcome 往返一致"
        assert loaded.evidence == "user_feedback", "evidence 往返一致"
        assert loaded.revision == 2, "revision 往返一致"
        assert loaded.judged_by == "external", "judged_by 往返一致"
        assert loaded.evidence_msgs == [1, 3], "evidence_msgs 往返一致"
        assert loaded.derived_ids == ["e1", "e2"], "derived_ids 往返一致"
        assert loaded.coverage_gap is True, "coverage_gap 往返一致"

    @staticmethod
    def test_load_missing_returns_none():
        store = VerdictStore(InMemoryKVStore())
        assert store.load(SCOPE, AUTHOR, "missing") is None, "不存在返回 None"

    @staticmethod
    def test_overwrite_updates_revision():
        store = VerdictStore(InMemoryKVStore())
        first = VerdictRecord(outcome="failure", evidence="model_only")
        store.save(SCOPE, AUTHOR, "t1", first)
        second = VerdictRecord(outcome="success", evidence="user_feedback", revision=1)
        store.save(SCOPE, AUTHOR, "t1", second)
        loaded = store.load(SCOPE, AUTHOR, "t1")
        assert loaded is not None and loaded.outcome == "success", "覆写保存应更新判定"
        assert loaded.revision == 1, "修订号覆写后一致"


def test_trajectory_ttl_constant():
    assert TRAJECTORY_TTL_SECONDS == 30 * 24 * 3600, "轨迹保留期 30 天"


def test_trajectory_expires_at_in_future():
    expires = datetime.fromisoformat(trajectory_expires_at())
    assert expires > datetime.now(UTC), "过期时间从当前时刻起算"


# ====================================================================== #
# 报错规则：未恢复判定的三种情形与恢复匹配的边界
# ====================================================================== #
# 恢复匹配的边界（同工具名 + 主要参数相等或包含）直接决定一条轨迹会不会被判成
# ``tool_error`` 级失败：包含匹配太宽（短命令误匹配）会把已恢复的报错误判为失败，
# 太严（严格相等）会把 t-0001 这类「包裹原命令重跑」的修复判成未恢复——两侧都测。


def _call(call_id: str, name: str, arguments: str) -> dict:
    """assistant 消息携带的一次工具调用。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "function": {"name": name, "arguments": arguments}}
        ],
    }


def _tool(call_id: str, content: str, *, is_error: object = False) -> dict:
    """tool 消息；is_error 用 object 宽松类型以覆盖字符串形态。"""
    message: dict = {"role": "tool", "tool_call_id": call_id, "content": content}
    if is_error is not None:
        message["is_error"] = is_error
    return message


class TestUnresolvedToolErrors:
    @staticmethod
    def test_no_tool_errors_returns_empty():
        messages = [
            _call("c1", "bash", '{"command": "ls"}'),
            _tool("c1", "file1\nfile2"),
            {"role": "assistant", "content": "done"},
        ]
        assert unresolved_tool_errors(messages) == [], "无报错的轨迹不应有未恢复下标"

    @staticmethod
    def test_error_without_retry_is_unresolved():
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config/app.yaml not found", is_error=True),
            {"role": "assistant", "content": "gave up"},
        ]
        assert unresolved_tool_errors(messages) == [1], "报错后无同调用成功应判未恢复"

    @staticmethod
    def test_recovered_by_exact_same_arguments():
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config/app.yaml not found", is_error=True),
            _call("c2", "bash", '{"command": "python -m app"}'),
            _tool("c2", "started"),
        ]
        assert unresolved_tool_errors(messages) == [], "同参数重跑成功即已恢复"

    @staticmethod
    def test_t0001_recovered_by_containment():
        """§2.2 t-0001：先 cp 再重跑，修复命令包裹原命令。"""
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config/app.yaml not found", is_error=True),
            _call(
                "c2",
                "bash",
                '{"command": "cp config/app.example.yaml config/app.yaml && python -m app"}',
            ),
            _tool("c2", "started"),
        ]
        assert unresolved_tool_errors(messages) == [], "包裹原命令的重跑成功应视为已恢复"

    @staticmethod
    def test_containment_requires_min_length():
        """短于 _CONTAINMENT_MIN_LEN 的主要参数不做包含匹配（防 ``ls`` 误匹配）。"""
        messages = [
            _call("c1", "bash", '{"command": "ls"}'),
            _tool("c1", "no such file", is_error=True),
            _call("c2", "bash", '{"command": "ls -la /opt/deploy"}'),
            _tool("c2", "total 0"),
        ]
        assert unresolved_tool_errors(messages) == [1], (
            "短命令的子串命中不算同一调用成功，报错应保持未恢复"
        )

    @staticmethod
    def test_same_arguments_but_different_tool_not_recovered():
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config/app.yaml not found", is_error=True),
            _call("c2", "run_script", '{"command": "python -m app"}'),
            _tool("c2", "started"),
        ]
        assert unresolved_tool_errors(messages) == [1], "工具名不同不算同一调用"

    @staticmethod
    def test_error_before_recovery_and_error_after_both_listed():
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config missing", is_error=True),
            _call("c2", "bash", '{"command": "python -m app"}'),
            _tool("c2", "still failing", is_error=True),
        ]
        assert unresolved_tool_errors(messages) == [1, 3], (
            "后继成功的调用本身也报错时，两侧报错都未恢复"
        )

    @staticmethod
    def test_later_failed_call_does_not_recover():
        """同参数的后续调用再次报错，不构成恢复。"""
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config missing", is_error=True),
            _call("c2", "bash", '{"command": "python -m app"}'),
            _tool("c2", "config still missing", is_error=True),
            {"role": "assistant", "content": "gave up"},
        ]
        assert unresolved_tool_errors(messages) == [1, 3], "后续同调用仍报错不恢复前错"

    @staticmethod
    def test_non_json_arguments_fall_back_to_raw_string():
        """arguments 非法 JSON 时取原串做主要参数。"""
        raw_cmd = "python -m app --config x.yaml"
        messages = [
            _call("c1", "bash", raw_cmd),
            _tool("c1", "config missing", is_error=True),
            _call("c2", "bash", raw_cmd),
            _tool("c2", "started"),
        ]
        assert unresolved_tool_errors(messages) == [], "原串相等的重跑成功即已恢复"

    @staticmethod
    def test_is_error_accepts_string_truthy_forms():
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config missing", is_error="true"),
            {"role": "assistant", "content": "gave up"},
        ]
        assert unresolved_tool_errors(messages) == [1], "is_error 字符串 true 应识别为报错"

    @staticmethod
    def test_missing_call_index_falls_back_to_empty_primary():
        """tool 消息没有对应 tool_calls 时按空参数处理（只按工具名匹配，恒不恢复）。"""
        messages = [
            _tool("orphan", "config missing", is_error=True),
        ]
        assert unresolved_tool_errors(messages) == [0], "孤儿报错下标原样返回"


class TestHasUnresolvedError:
    @staticmethod
    def test_true_when_unresolved_exists():
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config missing", is_error=True),
        ]
        assert has_unresolved_error(messages) is True, "存在未恢复报错即构成失败依据"

    @staticmethod
    def test_false_when_recovered():
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config missing", is_error=True),
            _call("c2", "bash", '{"command": "python -m app"}'),
            _tool("c2", "started"),
        ]
        assert has_unresolved_error(messages) is False, "报错已恢复不构成失败依据"

    @staticmethod
    def test_false_when_no_errors():
        assert has_unresolved_error([]) is False, "空轨迹不构成失败依据"


class TestHasErrorMark:
    @staticmethod
    def test_true_includes_recovered_errors():
        """值得提炼的判定不区分已恢复：t-0001 的配置缺失同样值得提炼。"""
        messages = [
            _call("c1", "bash", '{"command": "python -m app"}'),
            _tool("c1", "config missing", is_error=True),
            _call("c2", "bash", '{"command": "cp a b && python -m app"}'),
            _tool("c2", "started"),
        ]
        assert has_error_mark(messages) is True, "已恢复的报错同样算「轨迹中有报错」"

    @staticmethod
    def test_false_without_errors():
        messages = [
            _call("c1", "bash", '{"command": "ls"}'),
            _tool("c1", "file1"),
        ]
        assert has_error_mark(messages) is False, "无报错轨迹不算「轨迹中有报错」"


# ====================================================================== #
# 轨迹消息：读取位置与压缩折叠
# ====================================================================== #
# 判定反馈取「最后一条用户消息」、成败判定取「最后一段结果」——取错位置会把
# 反馈判到请求上；压缩轨迹超限折叠只保留首尾，中间报错被折叠掉不影响结论，因为
# 结构判定都在报错规则一节里做、不用压缩结果。


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict:
    return {"role": "assistant", "content": content}


def _assistant_call(name: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "function": {"name": name, "arguments": "{}"}}
        ],
    }


class TestIsRole:
    @staticmethod
    def test_matching_role():
        assert is_role(_user("hi"), "user") is True, "角色匹配返回真"

    @staticmethod
    def test_non_mapping_is_false():
        assert is_role("raw string", "user") is False, "非 dict 消息恒为否"


class TestUserMessageReaders:
    @staticmethod
    def test_last_user_message():
        messages = [_user("first"), _assistant("hi"), _user("second")]
        assert last_user_message(messages) == (2, "second"), "取最后一条用户消息"

    @staticmethod
    def test_last_user_message_none():
        assert last_user_message([_assistant("hi")]) is None, "无用户消息返回 None"

    @staticmethod
    def test_first_user_message():
        messages = [_user("first"), _user("second")]
        assert first_user_message(messages) == "first", "取第一条用户消息"

    @staticmethod
    def test_first_user_message_empty_when_missing():
        assert first_user_message([]) == "", "无用户消息返回空串"


class TestAssistantResult:
    @staticmethod
    def test_last_assistant_reply_skips_tool_calls():
        messages = [
            _user("run"),
            _assistant_call("bash"),
            _tool("c1", "output"),
            _assistant("final answer"),
            _assistant_call("read_file"),
        ]
        assert last_assistant_reply(messages) == "final answer", (
            "带工具调用的 assistant 不构成结果"
        )

    @staticmethod
    def test_last_assistant_reply_skips_empty_content():
        messages = [_user("run"), _assistant(""), _assistant("done")]
        assert last_assistant_reply(messages) == "done", "空正文的 assistant 不构成结果"

    @staticmethod
    def test_last_assistant_reply_empty_when_missing():
        assert last_assistant_reply([_user("hi")]) == "", "无结果返回空串"


class TestLastPostResultUserMessage:
    @staticmethod
    def test_single_turn_has_no_post_result_user_message():
        """单轮轨迹：用户请求在所有结果之前，应返回 None。"""
        messages = [_user("run"), _assistant("done")]
        assert last_post_result_user_message(messages) is None, (
            "单轮轨迹无「结果之后」的用户消息"
        )

    @staticmethod
    def test_multi_turn_finds_last_followup():
        messages = [
            _user("run"),
            _assistant("done"),
            _user("now do more"),
            _assistant("more done"),
            _user("thanks"),
        ]
        assert last_post_result_user_message(messages) == 4, "取最后一条跟进的用户消息"

    @staticmethod
    def test_tool_call_assistant_is_not_result():
        """带 tool_calls 的 assistant 不算结果：其后的用户消息不算跟进。"""
        messages = [
            _user("run"),
            _assistant_call("bash"),
            _user("clarification"),
        ]
        assert last_post_result_user_message(messages) is None, (
            "工具调用不构成结果，其后的用户消息不算跟进"
        )


class TestCompressTrace:
    @staticmethod
    def test_tool_line_carries_error_mark():
        trace = compress_trace(
            [_assistant_call("bash"), _tool("c1", "boom", is_error=True)]
        )
        assert "(ERROR)" in trace, "报错的 tool 行应带 (ERROR) 标记"

    @staticmethod
    def test_tool_line_without_error_has_no_mark():
        trace = compress_trace([_assistant_call("bash"), _tool("c1", "output")])
        assert "(ERROR)" not in trace, "未报错的 tool 行不带标记"

    @staticmethod
    def test_tool_line_truncated_to_chars_limit():
        trace = compress_trace([_tool("c1", "x" * 500)])
        assert "x" * 500 not in trace, "超长正文应被截断"

    @staticmethod
    def test_assistant_with_tools_lists_names():
        trace = compress_trace([_assistant_call("bash"), _assistant_call("read_file")])
        assert "bash" in trace and "read_file" in trace, "工具调用只留工具名列表"

    @staticmethod
    def test_long_trace_folded_head_and_tail_kept():
        messages = []
        for i in range(60):
            messages.append(_user(f"message-{i}"))
        trace = compress_trace(messages)
        assert "message-0" in trace, "折叠保留开头"
        assert "message-59" in trace, "折叠保留结尾"
        assert "...(omitted)..." in trace, "中间折叠有省略标记"
        assert "message-30" not in trace, "中间行被折叠"

    @staticmethod
    def test_short_trace_not_folded():
        messages = [_user(f"m{i}") for i in range(5)]
        trace = compress_trace(messages)
        assert "...(omitted)..." not in trace, "不超限不折叠"


# ====================================================================== #
# 密钥清洗与脱敏检查：两条写入闸的命中与放行边界
# ====================================================================== #
# 密钥清洗防止明文凭据进存储（作者自己的空间也不行）；脱敏检查决定共享空间的
# 经验条目整条丢弃。两侧的「不误伤」同样重要：仓库相对路径、参数名不能被清洗或
# 误判命中，否则正常经验会被静默丢弃。


class TestScrubText:
    @staticmethod
    def test_openai_key_redacted():
        text = "export OPENAI_API_KEY=sk-abcdef0123456789abcdef"
        cleaned = scrub_text(text)
        assert "sk-abcdef0123456789abcdef" not in cleaned, "OpenAI key 应被替换"
        assert "[REDACTED]" in cleaned, "替换占位符应出现"

    @staticmethod
    def test_github_aws_slack_tokens_redacted():
        text = (
            "ghp_abcdefghijklmnopqrst\n"
            "AKIAIOSFODNN7EXAMPLE\n"
            "xoxb-1234567890abcdef"
        )
        cleaned = scrub_text(text)
        for token in ("ghp_abcdefghijklmnopqrst", "AKIAIOSFODNN7EXAMPLE", "xoxb-1234567890abcdef"):
            assert token not in cleaned, f"{token[:8]}… 应被替换"

    @staticmethod
    def test_authorization_header_value_redacted_prefix_kept():
        text = "Authorization: Bearer abcdefghijklmn123"
        cleaned = scrub_text(text)
        assert "abcdefghijklmn123" not in cleaned, "Bearer 凭据段应被替换"
        assert "Authorization" in cleaned, "头名应保留"

    @staticmethod
    def test_password_assignment_keeps_key_redacts_value():
        text = 'password = "s3cret-value"'
        cleaned = scrub_text(text)
        assert "password = " in cleaned, "键名应保留（清洗后轨迹仍可读）"
        assert "s3cret-value" not in cleaned, "值段应被替换"

    @staticmethod
    def test_json_api_key_assignment_redacted():
        text = '{"api_key": "abcd1234efgh5678"}'
        cleaned = scrub_text(text)
        assert "abcd1234efgh5678" not in cleaned, "JSON 赋值的值段应被替换"
        assert '"api_key":' in cleaned, "键名应保留"

    @staticmethod
    def test_plain_text_without_credentials_untouched():
        text = "cp config/app.example.yaml config/app.yaml && python -m app"
        assert scrub_text(text) == text, "无凭据文本应原样返回"

    @staticmethod
    def test_empty_text_untouched():
        assert scrub_text("") == "", "空串原样返回"


class TestScrubMessages:
    @staticmethod
    def test_recursive_clean_inside_nested_structures():
        messages = [
            {
                "role": "user",
                "content": "my key is sk-abcdef0123456789abcdef please help",
                "metadata": {"notes": ["token: ghp_abcdefghijklmnopqrst"]},
            }
        ]
        cleaned = scrub_messages(messages)
        blob = str(cleaned)
        assert "sk-abcdef0123456789abcdef" not in blob, "正文里的凭据应被清洗"
        assert "ghp_abcdefghijklmnopqrst" not in blob, "嵌套列表里的凭据应被清洗"
        assert cleaned[0]["role"] == "user", "结构键（角色）不应被改动"

    @staticmethod
    def test_tool_call_arguments_string_cleaned():
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "bash",
                            "arguments": (
                                '{"command": "curl -H '
                                '\\"Authorization: Bearer abcdefghijklmn123\\" http://x"}'
                            ),
                        },
                    }
                ],
            }
        ]
        cleaned = scrub_messages(messages)
        args = cleaned[0]["tool_calls"][0]["function"]["arguments"]
        assert "abcdefghijklmn123" not in args, "arguments JSON 串里的凭据应被清洗"
        assert "bash" == cleaned[0]["tool_calls"][0]["function"]["name"], "工具名不应被改动"

    @staticmethod
    def test_input_not_mutated():
        messages = [
            {"role": "user", "content": "key sk-abcdef0123456789abcdef"},
        ]
        original = messages[0]["content"]
        scrub_messages(messages)
        assert messages[0]["content"] == original, "清洗返回新列表，不应改动入参"

    @staticmethod
    def test_non_mapping_message_passed_through():
        assert scrub_messages(["raw"]) == ["raw"], "非 dict 消息原样保留"

    @staticmethod
    def test_non_string_values_untouched():
        messages = [{"role": "tool", "content": "ok", "tool_call_id": 7}]
        cleaned = scrub_messages(messages)
        assert cleaned[0]["tool_call_id"] == 7, "非字符串值应原样返回"


class TestSubjectIdentifiers:
    @staticmethod
    def test_collects_user_agent_and_author_name():
        ids = subject_identifiers(user="alice", agent="a1", author="user:alice")
        assert ids == ["alice", "a1"], "user/agent 维与作者名字段都应收集且保持顺序"

    @staticmethod
    def test_author_without_colon_kept_whole():
        assert subject_identifiers(author="alice") == ["alice"], "无冒号主体整段保留"

    @staticmethod
    def test_dedup_preserving_order():
        ids = subject_identifiers(user="alice", agent="alice")
        assert ids == ["alice"], "重复标识去重"

    @staticmethod
    def test_empty_inputs_return_empty():
        assert subject_identifiers() == [], "全空输入返回空列表"


class TestDesensitizationHit:
    @staticmethod
    def test_word_boundary_hit():
        assert desensitization_hit("deploy as alice to prod", ["alice"]) is True, (
            "主体标识作为独立词出现即命中"
        )

    @staticmethod
    def test_no_false_hit_on_longer_identifier():
        assert desensitization_hit("user alice10 logged in", ["alice"]) is False, (
            "词边界匹配：alice 不应命中 alice10"
        )

    @staticmethod
    def test_short_identifier_skipped():
        assert desensitization_hit("run u1 script", ["u1"]) is False, (
            "短于下限的标识跳过（命中噪声大于信号）"
        )

    @staticmethod
    def test_home_dir_form_hits_without_length_floor():
        assert desensitization_hit("copy from /home/u1/data", ["u1"]) is True, (
            "用户目录形态不套长度下限：u1 出现在 /home/u1 即命中"
        )

    @staticmethod
    def test_windows_user_dir_form_hits():
        assert desensitization_hit(r"cache at C:\Users\alice\app", ["alice"]) is True, (
            "Windows 用户目录形态同样命中"
        )

    @staticmethod
    def test_relative_repo_path_not_hit():
        assert desensitization_hit("edit config/app.yaml then restart", ["alice"]) is False, (
            "仓库相对路径不含主体标识，不应命中"
        )

    @staticmethod
    def test_empty_content_never_hits():
        assert desensitization_hit("", ["alice"]) is False, "空正文不命中"


# ====================================================================== #
# 作者摘要：HMAC 确定性与主体推导
# ====================================================================== #


class TestAuthorDigest:
    @staticmethod
    def test_deterministic_hmac_sha256():
        expected = hmac.new(
            b"deploy-key", b"user:u1", hashlib.sha256
        ).hexdigest()
        assert author_digest("user:u1", "deploy-key") == expected, "HMAC-SHA256 计算正确"

    @staticmethod
    def test_same_author_different_key_differs():
        d1 = author_digest("user:u1", "key-a")
        d2 = author_digest("user:u1", "key-b")
        assert d1 != d2, "不同部署密钥产出不同摘要"

    @staticmethod
    def test_digest_is_64_hex():
        digest = author_digest("agent:a1", "key")
        assert len(digest) == 64, "SHA256 hex 长度 64"
        try:
            int(digest, 16)
        except ValueError:
            pytest.fail("摘要应为 hex 可解析")


class TestAuthorPrincipal:
    @staticmethod
    def test_user_wins_over_agent():
        assert author_principal(user="u1", agent="a1") == "user:u1", "有终端用户取 user"

    @staticmethod
    def test_agent_when_no_user():
        assert author_principal(agent="a1") == "agent:a1", "无终端用户取 agent"

    @staticmethod
    def test_empty_when_neither():
        assert author_principal() == "", "两者皆空返回空串"


# ====================================================================== #
# 按作者清除的登记回调（mem2.0 §2.5.2「按作者清除」）
# ====================================================================== #


class TestMakeErasureCallback:
    @staticmethod
    def test_registers_owner_digest_in_every_experience_space():
        """被删空间的每个 owner 的摘要写进该组织每个经验空间的清除标记。"""
        utility = UtilityStore(InMemoryKVStore())
        callback = make_erasure_callback(
            utility, author_key="deploy-key", curated_spaces=["exp-a", "exp-b"]
        )

        callback(
            "acme",
            "personal",
            [Scope(org="acme", user="u1"), Scope(org="acme", agent="bot-1")],
        )

        expected = {
            author_digest("user:u1", "deploy-key"),
            author_digest("agent:bot-1", "deploy-key"),
        }
        for space in ("exp-a", "exp-b"):
            scope = Scope(org="acme", space=space)
            assert set(utility.load_erased_authors(scope)) == expected, (
                f"经验空间 {space} 登记了全部 owner 的摘要"
            )

    @staticmethod
    def test_skips_owners_without_principal():
        """owners 里没有 user/agent 维度的项（组织级归属）跳过——无作者主体。"""
        utility = UtilityStore(InMemoryKVStore())
        callback = make_erasure_callback(
            utility, author_key="k", curated_spaces=["exp"]
        )

        callback("acme", "team", [Scope(org="acme")])

        assert utility.load_erased_authors(Scope(org="acme", space="exp")) == [], (
            "无作者主体的 owner 不登记"
        )

    @staticmethod
    def test_org_scoped_and_org_from_call():
        """标记写在回调收到的 org 下的经验空间（owner 维度不参与空间名）。"""
        utility = UtilityStore(InMemoryKVStore())
        callback = make_erasure_callback(
            utility, author_key="k", curated_spaces=["exp"]
        )

        callback("other-org", "personal", [Scope(org="other-org", user="u1")])

        assert utility.load_erased_authors(Scope(org="other-org", space="exp")) == [
            author_digest("user:u1", "k")
        ], "标记落在被删空间所属组织（org）的经验空间"
        assert utility.load_erased_authors(Scope(org="acme", space="exp")) == [], (
            "其他组织的经验空间不受影响"
        )

    @staticmethod
    def test_idempotent_repeated_registration():
        """同 owner 重复登记幂等——标记键按摘要定，覆盖不叠加。"""
        utility = UtilityStore(InMemoryKVStore())
        callback = make_erasure_callback(
            utility, author_key="k", curated_spaces=["exp"]
        )
        owners = [Scope(org="acme", user="u1")]

        callback("acme", "personal", owners)
        callback("acme", "personal", owners)

        assert utility.load_erased_authors(Scope(org="acme", space="exp")) == [
            author_digest("user:u1", "k")
        ], "重复登记不产生重复标记"


# ====================================================================== #
# repro_unit 的运行时解析（v3 §2.4：auto 按落点选口径）
# ====================================================================== #


def _naming():
    """个人空间模板 ``u_{user}``、agent 模板 ``a_{agent}`` 的命名规则。"""
    from jiuwen_memory.construction.router import SpaceNaming

    return SpaceNaming(
        templates=(
            ("personal", "u_{user}"),
            ("agent", "a_{agent}"),
            ("project", "p_{project}"),
        ),
        fallback_class="personal",
    )


class TestResolvedReproUnit:
    @staticmethod
    def _experience(**curated_kwargs) -> ExperienceConfig:
        return ExperienceConfig(
            default_space="own",
            curated_spaces=(
                CuratedSpaceConfig(space="u_alice", skill=True, **curated_kwargs),
            ),
        )

    def test_explicit_values_pass_through(self):
        """显式配置的 user / trajectory 原样返回（不参与 auto 解析）。"""
        experience = self._experience(repro_unit=REPRO_UNIT_USER)
        assert (
            experience.resolved_repro_unit("u_alice", _naming())
            == REPRO_UNIT_USER
        ), "显式 user 原样"
        experience = self._experience(repro_unit=REPRO_UNIT_TRAJECTORY)
        assert (
            experience.resolved_repro_unit("u_alice", _naming())
            == REPRO_UNIT_TRAJECTORY
        ), "显式 trajectory 原样"

    def test_auto_writer_space_resolves_trajectory(self):
        """auto + 写入者自己的空间（fallback 模板形态）按轨迹计数。"""
        experience = self._experience()  # 缺省 repro_unit=auto

        assert (
            experience.resolved_repro_unit("u_alice", _naming())
            == REPRO_UNIT_TRAJECTORY
        ), "u_alice 由 u_{user} 渲出：单用户空间按轨迹"

    @staticmethod
    def test_auto_shared_space_resolves_user():
        """auto + 共享空间按用户计数（多个用户共用的空间）。"""
        experience = ExperienceConfig(
            default_space="exp-shared",
            curated_spaces=(
                CuratedSpaceConfig(space="exp-shared", skill=True),
            ),
        )

        assert (
            experience.resolved_repro_unit("exp-shared", _naming())
            == REPRO_UNIT_USER
        ), "共享空间不是写入者空间，按用户"

    @staticmethod
    def test_auto_without_table_falls_back_to_default_space():
        """无判定表（经验不经落点判定）时按 default_space：own 即写入者空间。"""
        own = ExperienceConfig(default_space="own")
        named = ExperienceConfig(default_space="exp-shared")

        assert own.resolved_repro_unit("u_alice", None) == REPRO_UNIT_TRAJECTORY, (
            "own 落点：经验全落写入者自己的空间，按轨迹"
        )
        assert (
            named.resolved_repro_unit("exp-shared", None) == REPRO_UNIT_USER
        ), "具名落点：共享空间按用户"

    def test_space_not_in_curated_list_resolves_as_auto(self):
        """未列入 curated_spaces 的空间都按 auto 解析（§2 部署配置）。"""
        experience = self._experience()

        assert (
            experience.resolved_repro_unit("u_bob", _naming())
            == REPRO_UNIT_TRAJECTORY
        ), "清单外的 u_bob 仍按 auto：写入者自己的空间形态"
        assert (
            experience.resolved_repro_unit("p_agent-memory", _naming())
            == REPRO_UNIT_USER
        ), "清单外的项目空间按 user"


class TestIsFallbackSpace:
    @staticmethod
    def test_template_with_placeholder():
        naming = _naming()
        assert naming.is_fallback_space("u_alice") is True, "u_{user} 渲出的形态命中"
        assert naming.is_fallback_space("u_") is False, "占位符段非空才命中"
        assert naming.is_fallback_space("exp-shared") is False, "共享空间不命中"
        assert naming.is_fallback_space("a_bot-1") is False, (
            "fallback 类别是 personal：agent 模板的空间不是回落形态"
        )

    @staticmethod
    def test_constant_template_exact_match():
        """无占位符的常量模板退化为全等比较。"""
        from jiuwen_memory.construction.router import SpaceNaming

        naming = SpaceNaming(
            templates=(("personal", "personal-pool"),),
            fallback_class="personal",
        )
        assert naming.is_fallback_space("personal-pool") is True, "常量模板全等命中"
        assert naming.is_fallback_space("personal-pool-x") is False, "前缀不等于命中"

    @staticmethod
    def test_no_fallback_class_never_matches():
        """未声明 fallback 类别时恒不命中（没有可回落的空间形态）。"""
        from jiuwen_memory.construction.router import SpaceNaming

        naming = SpaceNaming(templates=(("project", "p_{project}"),))
        assert naming.is_fallback_space("u_alice") is False, "无 fallback 类别不命中"
        assert naming.is_fallback_space("") is False, "空空间名不命中"
