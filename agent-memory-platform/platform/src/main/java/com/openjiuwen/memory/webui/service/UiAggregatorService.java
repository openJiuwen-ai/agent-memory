package com.openjiuwen.memory.webui.service;

import java.util.Map;

/**
 * Web UI 聚合服务（§8.2）。
 * <p>
 * 将原 UiController 中的聚合逻辑抽取为独立 Service，
 * 控制器仅做 HTTP 参数接收与委托调用。
 */
public interface UiAggregatorService {

    /**
     * §8.2.2 记忆浏览页聚合数据
     */
    Map<String, Object> buildMemoryBrowse(String scopeId, String userId, String memoryType, int pageSize, int pageIdx);

    /**
     * §8.2.3 配置管理页聚合数据
     * <p>
     * 2026-07-16 P0-1.5：{@code scopeName} (VARCHAR) → {@code scopeId} (UUID)。
     */
    Map<String, Object> buildConfigPage(String adminUserId, String scopeId);

    /**
     * §8.2.4 日志页聚合数据
     */
    Map<String, Object> buildLogsPage(String adminUserId, String tab, int page, int size);

    /**
     * §8.2.5 运维页聚合数据
     */
    Map<String, Object> buildOpsPage(String adminUserId, String scopeId, String userId);

    /**
     * §8.2.7 记忆追溯页聚合数据
     */
    Map<String, Object> buildTracePage(String memId, String userId, String scopeId);
}
