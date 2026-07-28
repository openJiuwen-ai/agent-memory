package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.common.client.dto.MemoryItem;

import java.util.List;
import java.util.Map;

/** 运维工具集（功能4）。 */
public interface OpsToolService {

    List<MemoryItem> searchMemory(String query, int num, String userId, String scopeId, Double threshold);

    List<MemoryItem> searchSummary(String query, int num, String userId, String scopeId, Double threshold);

    Map<String, String> viewVariables(String userId, String scopeId, List<String> names);

    /** 健康探针（浅状态，:8516 /health） */
    Map<String, Object> healthProbe();

    /**
     * 记忆总数探测：:8516 的 total=当前页条数，无全局总数接口；
     * 降级为全量翻页累加 len(results) 直至某页 < page_size（昂贵，建议异步/缓存）。
     */
    Map<String, Object> memoryCount(String userId, String scopeId, String memoryType);

    /** 清空 scope 预演：统计将影响条数 + 签发 confirmToken */
    Map<String, Object> purgeScopePreview(String scopeId);

    /** 清空 scope 执行（二次确认） */
    Object purgeScope(String scopeId, String confirmToken, String operator, String reason);
}
