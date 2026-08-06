package com.openjiuwen.memory.webui;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.webui.service.UiAggregatorService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * Web UI 聚合 API 控制器（§8.2）。
 * <p>
 * 薄聚合层 — 仅接收 HTTP 参数并委托 {@link UiAggregatorService} 组装数据。
 * 所有业务逻辑已抽取至 UiAggregatorServiceImpl。
 * <p>
 * 已有端点（不在本控制器重复）：
 * <ul>
 *   <li>GET /api/v1/ui/governance/page — GovernanceController 已实现</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/v1/ui")
public class UiController {

    private final UiAggregatorService uiAggregatorService;

    public UiController(UiAggregatorService uiAggregatorService) {
        this.uiAggregatorService = uiAggregatorService;
    }

    // —— §8.2.2 记忆浏览页 ——

    @GetMapping("/memory/browse")
    public ApiResponse<Map<String, Object>> memoryBrowse(
            @RequestParam(name = "scope_id", required = false) String scopeId,
            @RequestParam(name = "user_id", required = false) String userId,
            @RequestParam(name = "memory_type", required = false) String memoryType,
            @RequestParam(name = "page_size", defaultValue = "20") int pageSize,
            @RequestParam(name = "page_idx", defaultValue = "1") int pageIdx) {
        return ApiResponse.ok(uiAggregatorService.buildMemoryBrowse(scopeId, userId, memoryType, pageSize, pageIdx));
    }

    // —— §8.2.3 配置管理页 ——

    @GetMapping("/config/page")
    public ApiResponse<Map<String, Object>> configPage(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "scope_id", required = false) String scopeId) {
        return ApiResponse.ok(uiAggregatorService.buildConfigPage(adminUserId, scopeId));
    }

    // —— §8.2.4 日志页 ——

    @GetMapping("/logs/page")
    public ApiResponse<Map<String, Object>> logsPage(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "tab", defaultValue = "operations") String tab,
            @RequestParam(name = "page", defaultValue = "1") int page,
            @RequestParam(name = "size", defaultValue = "20") int size) {
        return ApiResponse.ok(uiAggregatorService.buildLogsPage(adminUserId, tab, page, size));
    }

    // —— §8.2.5 运维页 ——

    @GetMapping("/ops/page")
    public ApiResponse<Map<String, Object>> opsPage(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "scope_id", required = false) String scopeId,
            @RequestParam(name = "user_id", required = false) String userId) {
        return ApiResponse.ok(uiAggregatorService.buildOpsPage(adminUserId, scopeId, userId));
    }

    // —— §8.2.7 记忆追溯页 ——

    @GetMapping("/trace/page")
    public ApiResponse<Map<String, Object>> tracePage(
            @RequestParam(name = "mem_id", required = false) String memId,
            @RequestParam(name = "user_id", required = false) String userId,
            @RequestParam(name = "scope_id", required = false) String scopeId) {
        return ApiResponse.ok(uiAggregatorService.buildTracePage(memId, userId, scopeId));
    }
}
