package com.openjiuwen.memory.opscenter.service;

import java.util.Map;

/** 功能7 — 记忆追溯。读 memory_change_log_snapshot 快照 + :8516 当前态组装追溯 bundle。 */
public interface TraceService {

    /** 全链路 bundle：current_state / source_messages / change_history / audit_trail / lineage。
     *  content/memType/timestamp/sourceId 由前端传入，避免后端翻页查找。 */
    Map<String, Object> getBundle(String memId, String userId, String scopeId,
                                  String content, String memType, String timestamp, String sourceId);

    /** 变更历史（CREATE/UPDATE 时间线）。 */
    Map<String, Object> getHistory(String memId);

    /** 操作审计链路（含 DELETE）。 */
    Map<String, Object> getAudit(String memId);
}
