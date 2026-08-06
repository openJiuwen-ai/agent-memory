package com.openjiuwen.memory.configcenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.configcenter.dto.KernelConfigUpdateRequest;
import com.openjiuwen.memory.configcenter.dto.KernelConfigUpdateResultDTO;
import com.openjiuwen.memory.configcenter.service.KernelConfigService;
import com.openjiuwen.memory.common.spi.ConfirmTokenService;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * 内核配置管理 Controller — Push 模型。
 * <p>
 * 遵循设计文档 §5.3 Push 模型：
 * <ul>
 *   <li>GET /api/v1/config/kernel — 获取内核当前配置（脱敏展示，只读）</li>
 *   <li>PUT /api/v1/config/kernel — 修改可热生效参数（Push 到内核 + 重启）</li>
 *   <li>GET /api/v1/config/kernel/confirm-token — 获取二次确认令牌（Layer 3）</li>
 * </ul>
 * <p>
 * 核心原则：内核始终是唯一配置源，服务层是内核的"远程编辑器"。
 * <p>
 * <b>三层安全加固：</b>
 * <ul>
 *   <li>Layer 1（代码层）：PUT /kernel 强制校验 config:write 权限；restart=true 时额外校验 kernel:restart 权限</li>
 *   <li>Layer 2（视觉层）：前端根据 config:write / kernel:restart 权限隐藏编辑/重启按钮</li>
 *   <li>Layer 3（流程层）：restart=true 时必须携带 confirmToken，服务层校验+消费（防重放）</li>
 * </ul>
 * <p>
 * 权限要求：
 * <ul>
 *   <li>config:read — 查看内核配置</li>
 *   <li>config:write — 修改内核可热生效参数</li>
 *   <li>kernel:restart — 触发内核重启（仅 SUPER_ADMIN / PLATFORM_ADMIN）</li>
 * </ul>
 * 租户管理员（SCOPE_ADMIN）有 config:write 但<b>无</b> kernel:restart，可改参数但不能触发重启。
 * <p>
 * 2026-07-19 P0-3 v3：内核配置页只读展示安装参数 + 连接参数。
 * 可修改参数拆分为热启动模板（tpl_instance_hot，立即生效）与冷启动模板
 * （tpl_instance_cold，需重启），由 ConfigTemplateController 管理。
 */
@RestController
@RequestMapping("/api/v1/config")
public class KernelConfigController {

    private static final String ACTION_KERNEL_RESTART = "KERNEL_RESTART";
    private static final String RESOURCE_KERNEL = "kernel";

    private final KernelConfigService service;
    private final PermissionChecker permissionChecker;
    private final TenantContextProvider tenantContextProvider;
    private final ConfirmTokenService confirmTokenService;

    public KernelConfigController(KernelConfigService service,
                                  PermissionChecker permissionChecker,
                                  TenantContextProvider tenantContextProvider,
                                  ConfirmTokenService confirmTokenService) {
        this.service = service;
        this.permissionChecker = permissionChecker;
        this.tenantContextProvider = tenantContextProvider;
        this.confirmTokenService = confirmTokenService;
    }

    /**
     * 获取内核当前配置（只读，敏感字段脱敏）。
     * <p>
     * 代理调用内核 GET /admin/config，每个参数附带 editable / category / danger 标记。
     */
    @GetMapping("/kernel")
    public ApiResponse<Map<String, Object>> getKernelConfig() {
        // Layer 1: 代码层权限拦截 — 即使 JWT 有效也必须通过
        permissionChecker.require("config:read");
        return ApiResponse.ok(service.getKernelConfig());
    }

    /**
     * 获取二次确认令牌（Layer 3 — 流程层保障）。
     * <p>
     * 前端在提交 restart=true 的配置修改前，先调用此接口获取令牌。
     * 令牌一次性使用，服务层在 PUT /kernel 中校验后消费（防重放）。
     */
    @GetMapping("/kernel/confirm-token")
    public ApiResponse<Map<String, String>> issueConfirmToken() {
        // Layer 1: 签发令牌也需要 config:write 权限
        permissionChecker.require("config:write");
        String operator = resolveOperator();
        String token = confirmTokenService.issue(operator, ACTION_KERNEL_RESTART, RESOURCE_KERNEL);
        return ApiResponse.ok(Map.of("confirmToken", token));
    }

    /**
     * 修改内核可热生效参数（Push 模型）。
     * <p>
     * 安装参数与连接参数为只读，拒绝修改；可修改参数请到配置模板的热启动/冷启动模板。
     * <p>
     * 流程：
     * <ol>
     *   <li>Layer 1: 校验 config:write 权限（代码层拦截）</li>
     *   <li>Layer 1: 若 restart=true，额外校验 kernel:restart 权限（租户管理员被拦截）</li>
     *   <li>Layer 3: 若 restart=true，校验 confirmToken（流程层拦截，防重放）</li>
     *   <li>过滤只读参数（安装参数 + 连接参数，拒绝修改）</li>
     *   <li>调用内核 PUT /admin/config 写入</li>
     *   <li>调用内核 POST /admin/restart 触发重启（若 restart=true 且权限通过）</li>
     *   <li>记录审计日志</li>
     * </ol>
     */
    @PutMapping("/kernel")
    public ApiResponse<KernelConfigUpdateResultDTO> updateKernelConfig(
            @Valid @RequestBody KernelConfigUpdateRequest request) {
        // Layer 1: 代码层权限拦截 — config:write 是修改配置的最低权限要求
        permissionChecker.require("config:write");

        // Layer 1: 若触发重启，额外要求 kernel:restart 权限（租户管理员无此权限，被拦截）
        if (request.isRestart()) {
            permissionChecker.require("kernel:restart");

            // Layer 3: 流程层 — 校验二次确认令牌
            String token = request.getConfirmToken();
            if (token == null || token.isBlank()) {
                throw new com.openjiuwen.memory.common.exception.BizException(
                        com.openjiuwen.memory.common.ResultCode.CONFIRM_TOKEN_INVALID,
                        "重启操作需要二次确认令牌（confirmToken），请先调用 GET /api/v1/config/kernel/confirm-token 获取");
            }
            String operator = resolveOperator();
            if (!confirmTokenService.validate(token, operator, ACTION_KERNEL_RESTART, RESOURCE_KERNEL)) {
                throw new com.openjiuwen.memory.common.exception.BizException(
                        com.openjiuwen.memory.common.ResultCode.CONFIRM_TOKEN_INVALID,
                        "确认令牌无效或已过期");
            }
            // 消费令牌（防重放）
            confirmTokenService.consume(token);
        }

        String operator = resolveOperator();
        return ApiResponse.ok(service.updateKernelConfig(request, operator));
    }

    /**
     * 从租户上下文中解析操作人，替代硬编码 "system"。
     */
    private String resolveOperator() {
        TenantContextProvider.TenantContext ctx = tenantContextProvider.current();
        if (ctx != null && ctx.userId() != null && !ctx.userId().isBlank()) {
            return ctx.userId();
        }
        return "system";
    }
}
