package com.openjiuwen.memory.opscenter.service;

import java.util.Map;

/** 功能6 — 记忆治理（决策层 P2）。策略 + 扫描 + 合规 + 配额 + 治理页聚合。 */
public interface GovernanceService {

    /** 组装四类策略（lifecycle/quality/quota/compliance），含配额上限 + 当前用量。 */
    Map<String, Object> getStrategy(String adminUserId);

    /** 保存四类策略（拆成 4 条 policy + 1 条 quota）。 */
    void saveStrategy(String adminUserId, Map<String, Object> strategy, String operator);

    /** 质量扫描：duplicate(相似度)/stale(过期)/empty(空内容)。 */
    Map<String, Object> scan(String scanType, String userId, String scopeId, Double threshold);

    /** 合规扫描：扫变量名是否命中 forbidden_variables；auto_fix=true 调 :8516 delete_variables 清理。 */
    Map<String, Object> compliance(String userId, String scopeId, boolean autoFix);

    /** 治理页聚合 bundle：governance_summary / scan_results / compliance_status / quota_status。 */
    Map<String, Object> getGovernancePage(String adminUserId, String userId, String scopeId);
}
