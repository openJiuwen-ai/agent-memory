package com.openjiuwen.memory.configcenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.spi.ConfirmTokenService;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import org.springframework.web.bind.annotation.*;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 二次确认 Controller — 高危操作流程层（薄包装）。
 * <p>
 * 委托给 SPI 的 ConfirmTokenService（P0-2 真实实现替换 noop）。
 * <p>
 * 路径：
 * <ul>
 *   <li>POST /api/v1/confirm-tokens/issue — 签发（高危操作前调）</li>
 *   <li>POST /api/v1/confirm-tokens/validate — 校验（不消费）</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/v1/confirm-tokens")
public class ConfirmTokenController {

    private final ConfirmTokenService service;
    private final PermissionChecker permissionChecker;
    private final TenantContextProvider tenantContextProvider;

    public ConfirmTokenController(ConfirmTokenService service,
                                    PermissionChecker permissionChecker,
                                    TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.permissionChecker = permissionChecker;
        this.tenantContextProvider = tenantContextProvider;
    }

    /**
     * 签发二次确认令牌。
     * <p>
     * body: {action, resource}
     * response: {confirmToken, ttl_minutes}
     */
    @PostMapping("/issue")
    public ApiResponse<Map<String, Object>> issue(@RequestBody Map<String, Object> body) {
        permissionChecker.require("ops:write");
        String operator = resolveOperator();
        String action = (String) body.get("action");
        String resource = (String) body.getOrDefault("resource", "kernel");
        if (action == null || action.isBlank()) {
            throw new IllegalArgumentException("body.action 必填");
        }
        String token = service.issue(operator, action, resource);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("confirmToken", token);
        result.put("ttl_minutes", 5);
        return ApiResponse.ok(result);
    }

    /**
     * 校验 token（不消费）。
     */
    @PostMapping("/validate")
    public ApiResponse<Map<String, Object>> validate(@RequestBody Map<String, Object> body) {
        permissionChecker.require("ops:read");
        String token = (String) body.get("token");
        String action = (String) body.get("action");
        String resource = (String) body.getOrDefault("resource", "kernel");
        String operator = resolveOperator();
        boolean valid = service.validate(token, operator, action, resource);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", valid);
        if (!valid) {
            result.put("reason", "令牌无效或已过期");
        }
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
