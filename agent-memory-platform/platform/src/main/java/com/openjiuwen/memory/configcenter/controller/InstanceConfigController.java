package com.openjiuwen.memory.configcenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.configcenter.dto.InstanceConfigDTO;
import com.openjiuwen.memory.configcenter.service.InstanceConfigService;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * 实例级配置 Controller — 2026-07-17 P0-3 v2
 * <p>
 * 路径 /api/v1/instance-config
 */
@RestController
@RequestMapping("/api/v1/instance-config")
public class InstanceConfigController {

    private final InstanceConfigService service;
    private final PermissionChecker permissionChecker;
    private final TenantContextProvider tenantContextProvider;

    public InstanceConfigController(InstanceConfigService service,
                                    PermissionChecker permissionChecker,
                                    TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.permissionChecker = permissionChecker;
        this.tenantContextProvider = tenantContextProvider;
    }

    @GetMapping
    public ApiResponse<Object> get() {
        permissionChecker.require("config:read");
        return ApiResponse.ok(service.get());
    }

    @PutMapping
    public ApiResponse<InstanceConfigDTO> update(@RequestBody Map<String, String> body) {
        permissionChecker.require("config:write");
        return ApiResponse.ok(service.update(body.get("configJson"), resolveOperator()));
    }

    private String resolveOperator() {
        TenantContextProvider.TenantContext ctx = tenantContextProvider.current();
        if (ctx != null && ctx.userId() != null && !ctx.userId().isBlank()) {
            return ctx.userId();
        }
        return "system";
    }
}
