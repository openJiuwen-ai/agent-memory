# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Plugin-owned rule snapshot based on PR406 / harryCoder/jiuwen-memory mem2.0.

Rule categories and names follow the colleague module. This snapshot evolves
independently of jiuwen memory; high-noise categories are opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DetectRule:
    """Detection configuration; validators are fixed names, never executable YAML."""

    category: str
    pattern: str
    name: str
    enabled: bool = True
    validator: str | None = None
    group: str | int = 0


def luhn_ok(text: str) -> bool:
    """Validate ASCII card digits without rejecting the rest of a text's hits."""
    if not text or any(char not in "0123456789" for char in text):
        return False
    total = 0
    for index, char in enumerate(reversed(text)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def id_card_ok(text: str) -> bool:
    """Optional checksum validation; shape-only protection remains the default."""
    if len(text) != 18 or any(char not in "0123456789" for char in text[:17]):
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    total = sum(int(char) * weight for char, weight in zip(text[:17], weights))
    return "10X98765432"[total % 11] == text[-1].upper()


VALIDATORS = {"luhn": luhn_ok, "id_card_cn": id_card_ok}

# Injection is classified/audited only unless category_levels explicitly maps it.
# Defaults retain complete PEM blocks and existing credential capture groups.
DEFAULT_RULES = (
    DetectRule(
        category="credential.private_key",
        pattern=(
            "-----BEGIN (?P<kind>(?:RSA |EC |OPE"
            "NSSH |ENCRYPTED )?PRIVATE KEY)-----"
            "(?:(?!-----BEGIN |-----END )[\\s\\S])"
            "*-----END (?P=kind)-----"
        ),
        name="pem_private_key",
    ),
    DetectRule(
        category="credential.aws_key",
        pattern="(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])",
        name="aws_access_key",
    ),
    DetectRule(
        category="credential.github_pat",
        pattern="(?<![A-Za-z0-9])gh[pousr]_[0-9A-Za-z]{36}(?![A-Za-z0-9])",
        name="github_pat",
    ),
    DetectRule(
        category="credential.openai_key",
        pattern="(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])",
        name="openai_key",
    ),
    DetectRule(
        category="credential.google_api_key",
        pattern="(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9])",
        name="google_api_key",
    ),
    DetectRule(
        category="credential.slack_token",
        pattern="(?<![A-Za-z0-9])xox[baprs]-[0-9A-Za-z-]{10,}(?![A-Za-z0-9])",
        name="slack_token",
    ),
    DetectRule(
        category="credential.wechat_key",
        pattern="(?<![A-Za-z0-9])(?:wx[a-f0-9]{16}|[0-9a-f]{32})(?![A-Za-z0-9])",
        name="wechat_key",
        enabled=False,
    ),
    DetectRule(
        category="credential.jwt",
        pattern=(
            "(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{"
            "10,}\\.[A-Za-z0-9_-]{10,}\\.[A-Za-z0-"
            "9_-]{5,}(?![A-Za-z0-9_-])"
        ),
        name="jwt",
    ),
    DetectRule(
        category="credential.db_conn",
        pattern=(
            "(?:mysql|postgres(?:ql)?|mongodb(?:\\+srv)?|redis|amqp)://[^\\s:/]+:[^\\s@]+@[^\\s]+"
        ),
        name="db_connection_string",
    ),
    DetectRule(
        category="credential.bearer",
        pattern="(?i)\\bBearer\\s+(?P<value>[A-Za-z0-9._~+/-]{1,}=*)",
        name="bearer_token",
        group="value",
    ),
    DetectRule(
        category="pii.phone_cn",
        pattern="(?<!\\d)1[3-9]\\d{9}(?!\\d)",
        name="phone_cn",
    ),
    DetectRule(
        category="pii.email",
        pattern="(?i)(?<![a-z0-9._%+-])[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}(?![a-z0-9-])",
        name="email",
    ),
    DetectRule(
        category="pii.id_card_cn",
        pattern=(
            "(?<!\\d)[1-9]\\d{5}(?:18|19|20)\\d{2}("
            "?:0[1-9]|1[0-2])(?:0[1-9]|[12]\\d|3["
            "01])\\d{3}[\\dXx](?![A-Za-z0-9_])"
        ),
        name="id_card_cn",
    ),
    DetectRule(
        category="pii.bank_card",
        pattern="(?<!\\d)\\d{16,19}(?!\\d)",
        name="bank_card",
        validator="luhn",
    ),
    DetectRule(
        category="pii.plate_cn",
        pattern=(
            "[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领]"
            "[A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ"
            "-NP-Z0-9挂学警港澳]"
        ),
        name="plate_cn",
    ),
    DetectRule(
        category="pii.passport_cn",
        pattern="(?<![A-Za-z0-9])[EeGg]\\d{8}(?![A-Za-z0-9])",
        name="passport_cn",
    ),
    DetectRule(
        category="pii.officer_id",
        pattern="[一-龥]{1,4}字第\\d{6,10}号",
        name="officer_id",
    ),
    DetectRule(
        category="pii.social_security_cn",
        pattern="(?<!\\d)\\d{3}-\\d{7}-\\d{7}(?!\\d)|(?<!\\d)社保[卡号:：\\s]*\\d{15,18}(?!\\d)",
        name="social_security_cn",
    ),
    DetectRule(
        category="pii.ip_v4",
        pattern=(
            "(?<!\\d)(?:(?:25[0-5]|2[0-4]\\d|1\\d\\d"
            "|[1-9]?\\d)\\.){3}(?:25[0-5]|2[0-4]\\d"
            "|1\\d\\d|[1-9]?\\d)(?!\\d)"
        ),
        name="ip_v4",
        enabled=False,
    ),
    DetectRule(
        category="pii.ip_v6",
        pattern="(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![0-9A-Fa-f:])",
        name="ip_v6",
        enabled=False,
    ),
    DetectRule(
        category="pii.person_name_cn",
        pattern="(?<![\\u4e00-\\u9fa5])[\\u4e00-\\u9fa5]{2,3}(?:先生|女士|同学|老师)(?![\\u4e00-\\u9fa5])",
        name="person_name_with_title",
        enabled=False,
    ),
    DetectRule(
        category="pii.address_cn",
        pattern=(
            "(?:北京|上海|天津|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙"
            "江|安徽|福建|江西|山东|河南|湖北|湖南|广东|海南|四川|贵州|"
            "云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|宁夏|新疆|香港|澳"
            "门)(?:省|市|自治区|特别行政区)?[\\u4e00-\\u9fa5]"
            "{0,6}(?:市|地区|州|盟)[\\u4e00-\\u9fa5]{0,"
            "8}(?:区|县|旗|市)[\\u4e00-\\u9fa5]{0,12}("
            "?:路|街|道|巷|号|栋|楼|室|单元)"
        ),
        name="address_cn",
        enabled=False,
    ),
    DetectRule(
        category="injection.prompt",
        pattern=(
            "忽略(?:以上|之前|前面|上述|上面)(?:的)?(?:所有|全部|"
            "任何)?(?:指令|指示|规则|设定|提示词?|要求|约束)|igno"
            "re\\s+(?:all\\s+|any\\s+)?(?:previous|"
            "prior|above|earlier)\\s+(?:instructi"
            "ons?|prompts?|rules?|directions?|me"
            "ssages?|context)|disregard\\s+(?:all"
            "\\s+|any\\s+)?(?:previous|prior|above"
            ")\\s+(?:instructions?|prompts?|rules"
            "?|context)|你(?:现在|已经)?(?:是|扮演|充当)(?"
            ":一个)?(?:全新|新的?|不同)(?:的)?(?:角色|系统|助手"
            "|身份)|从现在起你(?:是|将(?:会)?是|要扮演)|system"
            "\\s*prompt\\s*(?::|：)\\s*\\S+|<\\/?(?:sy"
            "stem|assistant|instructions?)>|reve"
            "al\\s+(?:your|the)\\s+(?:system\\s*)?p"
            "rompt|(?:输出|打印|显示|泄露|透露)你(?:的)?(?:系"
            "统|初始|原始)(?:提示词?|指令|prompt)|(?:what|"
            "show)\\s+(?:is|are)\\s+your\\s+(?:syst"
            "em\\s*)?prompt"
        ),
        name="prompt_injection",
    ),
    DetectRule(
        category="credential.labeled",
        name="credential_value",
        pattern=(
            "(?i)(?:api[_ -]?key|(?:access|refre"
            "sh|auth)[_ -]?token|password|passwd"
            "|client[_ -]?secret|secret|private["
            '_ -]?key|密码|密钥)[\\"\']?\\s*[:=：]\\s*[\\"'
            "']?(?P<value>[^\\s\\\"',;，；}]{1,})"
        ),
        group="value",
    ),
    DetectRule(
        category="credential.labeled",
        name="credential_double_quoted",
        pattern=(
            "(?i)(?:api[_ -]?key|(?:access|refre"
            "sh|auth)[_ -]?token|password|passwd"
            "|client[_ -]?secret|secret|private["
            '_ -]?key|密码|密钥)[\\"\']?\\s*[:=：]\\s*"(?'
            'P<value>(?:\\\\.|[^"\\\\])+)"'
        ),
        group="value",
    ),
    DetectRule(
        category="credential.labeled",
        name="credential_single_quoted",
        pattern=(
            "(?i)(?:api[_ -]?key|(?:access|refre"
            "sh|auth)[_ -]?token|password|passwd"
            "|client[_ -]?secret|secret|private["
            "_ -]?key|密码|密钥)[\\\"']?\\s*[:=：]\\s*'(?"
            "P<value>(?:\\\\.|[^'\\\\])+)'"
        ),
        group="value",
    ),
)
