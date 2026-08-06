package com.openjiuwen.memory.opscenter.service.impl;

import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.dto.GetUserMemByPageRequest;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.client.dto.RawResponses;
import com.openjiuwen.memory.common.client.dto.SearchHistorySummaryRequest;
import com.openjiuwen.memory.common.client.dto.SearchMemoryRequest;
import com.openjiuwen.memory.common.client.dto.GetVariablesRequest;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.opscenter.service.OpsToolService;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
public class OpsToolServiceImpl implements OpsToolService {

    private final MemoryEngineClient client;
    private final PermissionChecker permissionChecker;

    public OpsToolServiceImpl(MemoryEngineClient client, PermissionChecker permissionChecker) {
        this.client = client;
        this.permissionChecker = permissionChecker;
    }

    @Override
    public List<MemoryItem> searchMemory(String query, int num, String userId, String scopeId, Double threshold) {
        permissionChecker.require("memory:read");
        SearchMemoryRequest req = new SearchMemoryRequest();
        req.setQuery(query);
        req.setNum(num);
        req.setUserId(userId);
        req.setScopeId(scopeId);
        req.setThreshold(threshold);
        return client.searchMemory(req);
    }

    @Override
    public List<MemoryItem> searchSummary(String query, int num, String userId, String scopeId, Double threshold) {
        permissionChecker.require("memory:read");
        SearchHistorySummaryRequest req = new SearchHistorySummaryRequest();
        req.setQuery(query);
        req.setNum(num);
        req.setUserId(userId);
        req.setScopeId(scopeId);
        req.setThreshold(threshold);
        return client.searchHistorySummary(req);
    }

    @Override
    public Map<String, String> viewVariables(String userId, String scopeId, List<String> names) {
        permissionChecker.require("memory:read");
        GetVariablesRequest req = new GetVariablesRequest();
        req.setUserId(userId);
        req.setScopeId(scopeId);
        req.setNames(names);
        return client.getVariables(req);
    }

    @Override
    public Map<String, Object> healthProbe() {
        permissionChecker.require("ops:read");
        RawResponses.Health h = client.health();
        return Map.of("status", h.getStatus(), "message", h.getMessage() == null ? "" : h.getMessage());
    }

    @Override
    public Map<String, Object> memoryCount(String userId, String scopeId, String memoryType) {
        permissionChecker.require("memory:read");
        // 降级：全量翻页累加（total 不可信）。为避免无限调用，设上限。
        int pageSize = 100;
        int maxPages = 1000;
        long count = 0;
        int pageIdx = 1;
        while (pageIdx <= maxPages) {
            GetUserMemByPageRequest req = new GetUserMemByPageRequest();
            req.setUserId(userId);
            req.setScopeId(scopeId);
            req.setMemoryType(memoryType);
            req.setPageSize(pageSize);
            req.setPageIdx(pageIdx);
            List<MemoryItem> items = client.getUserMemByPage(req).items();
            count += items.size();
            if (items.size() < pageSize) {
                break;
            }
            pageIdx++;
        }
        return Map.of("count", count, "approximate", true,
                "hint", "记忆服务未暴露 user_mem_total_num，值为全量翻页累加");
    }

    @Override
    public Map<String, Object> purgeScopePreview(String scopeId) {
        permissionChecker.require("memory:read");
        // 统计将删除条数（降级：第一页 size 探测或全量计数；此处用 memoryCount）
        Map<String, Object> cnt = memoryCount(null, scopeId, null);
        String token = "ct_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        return Map.of(
                "scopeId", scopeId,
                "affectedCount", cnt.get("count"),
                "confirmToken", token,
                "expiresAt", "5m"
        );
    }

    @Override
    public Object purgeScope(String scopeId, String confirmToken, String operator, String reason) {
        permissionChecker.require("memory:delete");
        // 二次确认 + 删除交由 MemoryManageService.deleteByScope（复用 confirmToken 校验与留痕）
        throw new UnsupportedOperationException("TODO: 委托 MemoryManageService.deleteByScope(scopeId, confirmToken, operator, reason)");
    }
}
