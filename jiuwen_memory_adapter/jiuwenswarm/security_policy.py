"""Validated Swarm settings for the current detector and outbound boundaries."""

from dataclasses import dataclass

from ..agent_security._validation import has_exact_type
from ..agent_security.detectors.rules import DEFAULT_RULES
from ..agent_security.inspection import create_text_inspector

_LABELS = {
    "credential.private_key": "私钥",
    "credential.aws_key": "AWS 访问密钥",
    "credential.github_pat": "GitHub 访问令牌",
    "credential.openai_key": "OpenAI API Key",
    "credential.google_api_key": "Google API Key",
    "credential.slack_token": "Slack 令牌",
    "credential.wechat_key": "微信密钥",
    "credential.jwt": "JWT",
    "credential.db_conn": "数据库连接串",
    "credential.bearer": "Bearer 令牌",
    "credential.labeled": "带标签的密码、密钥或令牌",
    "pii.phone_cn": "中国大陆手机号",
    "pii.email": "邮箱",
    "pii.id_card_cn": "中国居民身份证",
    "pii.bank_card": "银行卡号",
    "pii.plate_cn": "中国机动车车牌",
    "pii.passport_cn": "中国护照号",
    "pii.officer_id": "军官证件号",
    "pii.social_security_cn": "中国社保号码",
    "pii.ip_v4": "IPv4 地址",
    "pii.ip_v6": "IPv6 地址",
    "pii.person_name_cn": "带称谓的中文姓名",
    "pii.address_cn": "中国地址",
    "injection.prompt": "提示注入文本",
}
_GROUPS = {"pii": "个人信息", "credential": "凭据", "injection": "提示注入"}
_LEVELS = {"pii": "S2", "credential": "S3", "injection": "observe"}
_CATEGORIES = tuple(dict.fromkeys(rule.category for rule in DEFAULT_RULES))


@dataclass(frozen=True)
class SwarmPolicySettings:
    model: bool
    tools: bool
    agents: bool
    # Each entry is (category, enabled, assigned level).
    categories: tuple[tuple[str, bool, str], ...]

    @classmethod
    def defaults(cls):
        active = {rule.category for rule in DEFAULT_RULES if rule.enabled}
        categories = tuple(
            (name, name in active, _LEVELS[name.partition(".")[0]]) for name in _CATEGORIES
        )
        return cls(True, True, True, categories)

    @classmethod
    def parse(cls, value):
        outlets = ("model", "tools", "agents")
        if not has_exact_type(value, dict) or value.keys() != {*outlets, "categories"}:
            raise ValueError("invalid protection policy")
        if not all(has_exact_type(value[key], bool) for key in outlets):
            raise ValueError("invalid memory use switch")
        categories = value["categories"]
        if not has_exact_type(categories, dict) or categories.keys() != set(_CATEGORIES):
            raise ValueError("unknown or missing detection category")
        parsed = []
        for name in _CATEGORIES:
            entry = categories[name]
            if not has_exact_type(entry, dict) or entry.keys() != {"enabled", "level"}:
                raise ValueError("invalid detection category")
            if not has_exact_type(entry["enabled"], bool):
                raise ValueError("invalid detection switch")
            levels = ("S1", "S2", "S3")
            if name.startswith("injection."):
                levels += ("observe",)
            if entry["level"] not in levels:
                raise ValueError("invalid detection level")
            parsed.append((name, entry["enabled"], entry["level"]))
        return cls(value["model"], value["tools"], value["agents"], tuple(parsed))

    def mapping(self):
        return {
            "model": self.model,
            "tools": self.tools,
            "agents": self.agents,
            "categories": {
                name: {"enabled": enabled, "level": level}
                for name, enabled, level in self.categories
            },
        }

    def inspector(self):
        enabled = {name for name, active, _ in self.categories if active}
        return create_text_inspector(
            category_levels={
                name: level for name, active, level in self.categories
                if active and level != "observe"
            },
            rule_overrides={
                rule.name: {"enabled": rule.category in enabled} for rule in DEFAULT_RULES
            },
        )


def category_catalog():
    return [
        {"id": name, "label": _LABELS[name], "group": _GROUPS[name.partition(".")[0]]}
        for name in _CATEGORIES
    ]
