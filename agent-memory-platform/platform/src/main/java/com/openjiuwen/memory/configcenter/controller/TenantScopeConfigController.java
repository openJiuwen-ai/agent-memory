package com.openjiuwen.memory.configcenter.controller;

import com.openjiuwen.memory.configcenter.dto.TenantScopeConfigDeleteResultDTO;
import com.openjiuwen.memory.configcenter.dto.TenantScopeConfigListItemDTO;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.configcenter.domain.TenantScopeConfigEntity;
import com.openjiuwen.memory.configcenter.dto.TenantScopeConfigDTO;
import com.openjiuwen.memory.configcenter.service.TenantScopeConfigService;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/**
 * 租户级 Scope 配置 Controller — 2026-07-17 P0-3 v2 重构
 * <p>
 * 路径前缀 /api/v1/tenant-scope-configs
 * <ul>
 *   <li>GET    /{tenantId}                查租户快照</li>
 *   <li>PUT    /{tenantId}                租户修改自己的参数</li>
 *   <li>GET    /                          列出所有租户快照</li>
 *   <li>GET    /deviated                  列出偏离模板的租户</li>
 *   <li>POST   /{tenantId}/sync-from-template  平台操作: 重新下发模板</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/v1/tenant-scope-configs")
public class TenantScopeConfigController {

    private final TenantScopeConfigService service;
    private final PermissionChecker permissionChecker;
    private final TenantContextProvider tenantContextProvider;

    public TenantScopeConfigController(TenantScopeConfigService service,
                                        PermissionChecker permissionChecker,
                                        TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.permissionChecker = permissionChecker;
        this.tenantContextProvider = tenantContextProvider;
    }

    @GetMapping("/{tenantId}")
    public ApiResponse<TenantScopeConfigDTO> getByTenant(@PathVariable String tenantId) {
        permissionChecker.require("config:read");
        return ApiResponse.ok(service.getByTenant(tenantId));
    }

    @PutMapping("/{tenantId}")
    public ApiResponse<TenantScopeConfigDTO> update(
            @PathVariable String tenantId,
            @RequestBody Map<String, String> body) {
        permissionChecker.require("config:write");
        return ApiResponse.ok(service.update(tenantId, body.get("configJson"), resolveOperator()));
    }

    /**
     * 列出某模板下的租户快照（不含 config_json 大字段）。
     * templateId 必填：避免前端"忘传 → 拿全量 → 内存过滤"的反模式。
     */
    @GetMapping
    public ApiResponse<List<TenantScopeConfigListItemDTO>> listAll(
            @RequestParam(required = true) String templateId) {
        permissionChecker.require("config:read");
        return ApiResponse.ok(service.listAll(templateId));
    }

    @GetMapping("/deviated")
    public ApiResponse<List<TenantScopeConfigListItemDTO>> listDeviated() {
        permissionChecker.require("config:read");
        return ApiResponse.ok(service.listDeviated());
    }

    @PostMapping("/{tenantId}/sync-from-template")
    public ApiResponse<TenantScopeConfigDTO> syncFromTemplate(@PathVariable String tenantId) {
        permissionChecker.require("config:write");
        return ApiResponse.ok(service.syncFromTemplate(tenantId, resolveOperator()));
    }

    /**
     * 清除租户的 Scope 配置：删除内核 KV 中的 scope 配置 + DB 绑定记录，
     * 使该租户回退到默认配置。租户本身不删除。
     * <p>
     * 适用场景：在租户管理页移除某租户的自定义配置，让其走默认配置。
     */
    @DeleteMapping("/{tenantId}")
    public ApiResponse<TenantScopeConfigDeleteResultDTO> delete(@PathVariable String tenantId) {
        permissionChecker.require("config:write");
        return ApiResponse.ok(service.delete(tenantId, resolveOperator()));
    }

    private String resolveOperator() {
        TenantContextProvider.TenantContext ctx = tenantContextProvider.current();
        if (ctx != null && ctx.userId() != null && !ctx.userId().isBlank()) {
            return ctx.userId();
        }
        return "system";
    }
}
