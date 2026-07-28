package com.openjiuwen.memory.configcenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.configcenter.domain.ConfigTemplateEntity;
import com.openjiuwen.memory.configcenter.dto.ApplyTemplateRequest;
import com.openjiuwen.memory.configcenter.dto.ConfigTemplateListItemDTO;
import com.openjiuwen.memory.configcenter.dto.CreateTemplateRequest;
import com.openjiuwen.memory.configcenter.dto.TemplateApplyResultDTO;
import com.openjiuwen.memory.configcenter.dto.UpdateTemplateRequest;
import com.openjiuwen.memory.configcenter.service.ConfigTemplateService;
import org.springframework.web.bind.annotation.*;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 配置模板 Controller — 2026-07-17 P0-3 v2 重构
 * <p>
 * 路径前缀 /api/v1/config/templates
 * <ul>
 *   <li>GET    /?type=SCOPE&is_builtin=true     列出模板（附带 tenant_usage）</li>
 *   <li>GET    /{id}                            查模板详情</li>
 *   <li>POST   /                                创建模板（带 targetTenantIds 时同步应用）</li>
 *   <li>POST   /{sourceId}/copy                 复制模板</li>
 *   <li>PUT    /{id}                            修改模板（预置不可改）</li>
 *   <li>DELETE /{id}                            删除模板（预置不可删 + 无应用记录）</li>
 *   <li>POST   /apply                           应用模板到租户（SCOPE 必填 targetTenantIds）</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/v1/config/templates")
public class ConfigTemplateController {

    private final ConfigTemplateService service;
    private final PermissionChecker permissionChecker;
    private final TenantContextProvider tenantContextProvider;

    public ConfigTemplateController(ConfigTemplateService service,
                                     PermissionChecker permissionChecker,
                                     TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.permissionChecker = permissionChecker;
        this.tenantContextProvider = tenantContextProvider;
    }

    @GetMapping
    public ApiResponse<List<ConfigTemplateListItemDTO>> list(
            @RequestParam(value = "type", required = false) String type,
            @RequestParam(value = "is_builtin", required = false) Boolean isBuiltin) {
        permissionChecker.require("config:read");
        return ApiResponse.ok(service.list(type, isBuiltin));
    }

    @GetMapping("/{id}")
    public ApiResponse<ConfigTemplateEntity> get(@PathVariable String id) {
        permissionChecker.require("config:read");
        return ApiResponse.ok(service.get(id));
    }

    @PostMapping
    public ApiResponse<TemplateApplyResultDTO> create(@RequestBody CreateTemplateRequest request) {
        permissionChecker.require("config:write");
        return ApiResponse.ok(service.create(request, resolveOperator()));
    }

    @PostMapping("/{sourceId}/copy")
    public ApiResponse<TemplateApplyResultDTO> copy(
            @PathVariable String sourceId,
            @RequestBody CreateTemplateRequest request) {
        permissionChecker.require("config:write");
        return ApiResponse.ok(service.copy(sourceId, request, resolveOperator()));
    }

    @PutMapping("/{id}")
    public ApiResponse<ConfigTemplateEntity> update(
            @PathVariable String id,
            @RequestBody UpdateTemplateRequest request) {
        permissionChecker.require("config:write");
        return ApiResponse.ok(service.update(id, request, resolveOperator()));
    }

    @DeleteMapping("/{id}")
    public ApiResponse<Void> delete(@PathVariable String id) {
        permissionChecker.require("config:write");
        service.delete(id, resolveOperator());
        return ApiResponse.ok();
    }

    @PostMapping("/apply")
    public ApiResponse<TemplateApplyResultDTO> apply(@RequestBody ApplyTemplateRequest request) {
        permissionChecker.require("config:write");
        return ApiResponse.ok(service.apply(request, resolveOperator()));
    }

    /**
     * 比较多个模板的配置差异（V3 §4.5 API#12）。
     */
    @PostMapping("/compare")
    public ApiResponse<Map<String, Object>> compare(@RequestBody Map<String, Object> request) {
        permissionChecker.require("config:read");
        @SuppressWarnings("unchecked")
        List<String> templateIds = (List<String>) request.get("template_ids");
        if (templateIds == null || templateIds.size() < 2) {
            throw new BizException(ResultCode.BAD_REQUEST, "比较至少需要两个模板 ID");
        }
        Map<String, Object> result = new LinkedHashMap<>();
        List<Map<String, Object>> templates = new ArrayList<>();
        for (String id : templateIds) {
            Map<String, Object> info = new LinkedHashMap<>();
            try {
                ConfigTemplateEntity t = service.get(id);
                info.put("template_id", t.getId());
                info.put("template_name", t.getTemplateName());
                info.put("template_type", t.getTemplateType());
                info.put("config_json", t.getConfigJson());
            } catch (Exception e) {
                info.put("template_id", id);
                info.put("error", e.getMessage());
            }
            templates.add(info);
        }
        result.put("templates", templates);
        result.put("identical", templates.size() >= 2
                && templates.stream().allMatch(m -> m.get("config_json") != null)
                && templates.stream().map(m -> m.get("config_json")).distinct().count() == 1);
        return ApiResponse.ok(result);
    }

    private String resolveOperator() {
        TenantContextProvider.TenantContext ctx = tenantContextProvider.current();
        if (ctx != null && ctx.userId() != null && !ctx.userId().isBlank()) {
            return ctx.userId();
        }
        return "system";
    }
}
