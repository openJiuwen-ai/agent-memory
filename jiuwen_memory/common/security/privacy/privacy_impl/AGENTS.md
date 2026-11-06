# 隐私业务实现

## 模块地图

| 文件 | 职责 |
|---|---|
| `rule_patterns.py` | 移植的统一 DetectRule 目录、固定命名的 Luhn/身份证校验器 |
| `static_privacy_classifier.py` | 内置/自定义规则装配、类别映射、全部命中检测、PrivacyMark 与重叠合并 |
| `policy.py` | Level、SensitiveTaxonomy、DictPrivacyManager 的判级、继承与写入决策 |
| `regex.py` | PrivacyProtection 适配；原文摄入、持久化前可还原替换、PrivacyStore 关联及人类正文组装 |
| `__init__.py` | 注册 regex 提供者，不新增 Engine 或普通召回接口 |

## 行为铁律

- 分类和替换共用同一检测器、全部命中位置及逐命中校验；无效首项不得压过后续有效项。
- 用户规则与内置规则组合时按最高 rank 合并，不以先命中的用户规则降低凭据保护。
- 内置类别先精确映射，再选择最长前缀；未映射的 injection 只产生依据，不替换。
- 既有 rules 显式列表继续替换内置目录；use_builtin_rules=true 才明确请求叠加。
- 默认完整私钥块保护与身份证形态保护不弱化；高误报目录项默认关闭、显式开启。
- YAML 校验器只能选择固定名称，不加载 Python callable；失效配置和检测超时均拒绝。
- 分类依据最多保留 64 个规则名和类别的摘要，不含原值、关键词、正则正文或自由理由。
  两个内核字段不参与保护正文指纹，不构成授权或“已经脱敏”的客户端证明。
- 等级与依据只继承同完整 Scope 来源；总级别保留最高来源级别。
- 不移动脱敏时机，不实施不可逆优先替换；不在本目录重新实现认证、判权或密码学。

## 与其他子目录的边界

本目录保存规则、分类和策略业务及既有 PrivacyProtection 的适配。MemoryAPI 仍负责
资源 PEP，HumanDisclosure 仍负责人的证明及当前状态/版本复查，PrivacyStore 仍负责
私有事务与加解密。上云、分享、留存执行和 LLM 分类继续保留配置与明确的 TODO。
