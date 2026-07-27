package com.openjiuwen.memory.opscenter.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.exception.GapException;
import com.openjiuwen.memory.opscenter.domain.CommandExecutionLogEntity;
import com.openjiuwen.memory.opscenter.domain.OpsCommandCatalogEntity;
import com.openjiuwen.memory.opscenter.mapper.CommandExecutionLogMapper;
import com.openjiuwen.memory.opscenter.mapper.OpsCommandCatalogMapper;
import com.openjiuwen.memory.opscenter.service.OpsCommandService;
import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.common.spi.ConfirmTokenService;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

@Service
public class OpsCommandServiceImpl implements OpsCommandService {

    /**
     * 高危命令集合 — 这些命令需要 kernel:restart 权限 + 二次确认令牌。
     * 租户管理员（SCOPE_ADMIN）有 ops:write 但无 kernel:restart，会被拦截。
     */
    private static final Set<String> HIGH_RISK_COMMANDS = Set.of(
            "RESTART_KERNEL", "CLEAR_CACHE", "REBUILD_INDEX"
    );

    private static final String ACTION_KERNEL_RESTART = "KERNEL_RESTART";
    private static final String RESOURCE_KERNEL = "kernel";

    private final OpsCommandCatalogMapper catalogMapper;
    private final CommandExecutionLogMapper execMapper;
    private final MemoryEngineClient client;
    private final PermissionChecker permissionChecker;
    private final AuditRecorder auditRecorder;
    private final ConfirmTokenService confirmTokenService;
    private final ObjectMapper objectMapper;

    public OpsCommandServiceImpl(OpsCommandCatalogMapper catalogMapper,
                                 CommandExecutionLogMapper execMapper,
                                 MemoryEngineClient client,
                                 PermissionChecker permissionChecker,
                                 AuditRecorder auditRecorder,
                                 ConfirmTokenService confirmTokenService,
                                 ObjectMapper objectMapper) {
        this.catalogMapper = catalogMapper;
        this.execMapper = execMapper;
        this.client = client;
        this.permissionChecker = permissionChecker;
        this.auditRecorder = auditRecorder;
        this.confirmTokenService = confirmTokenService;
        this.objectMapper = objectMapper;
    }

    @Override
    public List<OpsCommandCatalogEntity> catalog(String category) {
        permissionChecker.check("ops:read");
        if (category == null || category.isBlank()) {
            return catalogMapper.selectList(null);
        }
        return catalogMapper.findByCategory(category);
    }

    @Override
    public Map<String, Object> dispatch(String commandCode, String scopeId, String userId,
                                        Map<String, Object> payload, boolean dryRun, String reason, String operator) {
        // Layer 1: 代码层权限拦截 — ops:write 是下发运维命令的最低权限要求
        permissionChecker.require("ops:write");

        OpsCommandCatalogEntity cmd = catalogMapper.selectById(commandCode);
        if (cmd == null) {
            throw new BizException(ResultCode.NOT_FOUND, "未知命令: " + commandCode);
        }

        // Layer 1: 高危命令额外校验 kernel:restart 权限
        // 租户管理员（SCOPE_ADMIN）有 ops:write 但无 kernel:restart，会被拦截
        if (HIGH_RISK_COMMANDS.contains(commandCode)) {
            permissionChecker.require("kernel:restart");
        }

        String executionId = "exec_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        long t0 = System.currentTimeMillis();

        // gap-ness 由 route() 实际调用决定：真实版 Client 缺口方法 default 抛 GapException → 转 gap；
        // mock 版 Client 返回成功 → 走通。catalog 的 enabled/gapReason 仅作 UI 提示，不再硬短路。
        // dryRun：回显将调用的接口，不实际下发
        if (dryRun) {
            return finish(executionId, commandCode, scopeId, userId, payload,
                    Map.of("endpoint", describeEndpoint(commandCode), "params", payload == null ? Map.of() : payload),
                    "dry_run", null, t0, operator, reason, false);
        }

        // Layer 3: 流程层 — 高危命令需二次确认令牌（防重放攻击）
        if (Boolean.TRUE.equals(cmd.getRequireConfirm()) || HIGH_RISK_COMMANDS.contains(commandCode)) {
            String token = payload == null ? null : (String) payload.get("confirmToken");
            if (token == null || token.isBlank()) {
                throw new BizException(ResultCode.CONFIRM_TOKEN_INVALID,
                        "高危命令需要二次确认令牌（confirmToken），请先获取令牌后重试");
            }
            if (!confirmTokenService.validate(token, operator, ACTION_KERNEL_RESTART, RESOURCE_KERNEL)) {
                throw new BizException(ResultCode.CONFIRM_TOKEN_INVALID,
                        "确认令牌无效或已过期");
            }
            // 消费令牌（防重放）
            confirmTokenService.consume(token);
        }

        // 路由到 Client（缺口命令在此抛 GapException，被全局处理器转 50010）
        Object result;
        try {
            result = route(commandCode, scopeId, userId, payload);
        } catch (GapException g) {
            return finish(executionId, commandCode, scopeId, userId, payload, null,
                    "gap", g.gapHint(), t0, operator, reason, true);
        }

        return finish(executionId, commandCode, scopeId, userId, payload, result,
                "success", null, t0, operator, reason, false);
    }

    @Override
    public CommandExecutionLogEntity execution(String executionId) {
        permissionChecker.check("ops:read");
        CommandExecutionLogEntity log = execMapper.selectById(executionId);
        if (log == null) {
            throw new BizException(ResultCode.NOT_FOUND, "执行记录不存在: " + executionId);
        }
        return log;
    }

    @Override
    public PageResult<CommandExecutionLogEntity> executions(int pageIdx, int pageSize, String commandCode, String status) {
        permissionChecker.check("ops:read");
        LambdaQueryWrapper<CommandExecutionLogEntity> w = new LambdaQueryWrapper<>();
        if (commandCode != null && !commandCode.isBlank()) w.eq(CommandExecutionLogEntity::getCommandCode, commandCode);
        if (status != null && !status.isBlank()) w.eq(CommandExecutionLogEntity::getStatus, status);
        w.orderByDesc(CommandExecutionLogEntity::getCreatedAt);
        List<CommandExecutionLogEntity> all = execMapper.selectList(w);
        int total = all.size();
        int from = Math.max(0, (pageIdx - 1) * pageSize);
        int to = Math.min(total, from + pageSize);
        List<CommandExecutionLogEntity> page = from < to ? all.subList(from, to) : List.of();
        return PageResult.of(page, total, pageIdx, pageSize);
    }

    // —— 路由：运维命令 = 对系统本身的管理操作（非记忆业务） ——
    private Object route(String code, String scopeId, String userId, Map<String, Object> payload) {
        return switch (code) {
            case "HEALTH_INSPECTION" -> client.health();
            case "RESTART_KERNEL" -> client.restartKernel();
            case "RELOAD_CONFIG" -> client.reloadConfig();
            case "CLEAR_CACHE" -> client.clearCache();
            case "REBUILD_INDEX" -> client.rebuildIndex();
            case "START_DREAMING" -> client.startDreaming(payload);
            case "STOP_DREAMING" -> client.stopDreaming(scopeId, userId);
            case "DREAMING_STATUS" -> client.dreamingStatus();
            case "MIGRATE_VECTOR" -> client.migrate(
                    payload == null ? Map.of() : (Map<String, Object>) payload.getOrDefault("source", Map.of()),
                    payload == null ? Map.of() : (Map<String, Object>) payload.getOrDefault("target", Map.of()));
            default -> throw new BizException(ResultCode.BAD_REQUEST, "无路由实现: " + code);
        };
    }

    private String describeEndpoint(String code) {
        return switch (code) {
            case "HEALTH_INSPECTION" -> "GET /health";
            case "RESTART_KERNEL" -> "POST /admin/restart (缺口)";
            case "CLEAR_CACHE" -> "POST /admin/clear-cache (缺口)";
            default -> "缺口端点";
        };
    }

    private Map<String, Object> finish(String executionId, String code, String scopeId, String userId,
                                       Map<String, Object> payload, Object result, String status,
                                       String gapHint, long t0, String operator, String reason, boolean persistOnly) {
        int duration = (int) (System.currentTimeMillis() - t0);
        // 持久化执行日志
        try {
            CommandExecutionLogEntity log = new CommandExecutionLogEntity();
            log.setExecutionId(executionId);
            log.setCommandCode(code);
            log.setScopeId(scopeId);
            log.setUserId(userId);
            log.setPayloadSnapshot(objectMapper.writeValueAsString(payload == null ? Map.of() : payload));
            log.setResultSnapshot(result == null ? null : objectMapper.writeValueAsString(result));
            log.setStatus(status);
            log.setGapHint(gapHint);
            log.setDurationMs(duration);
            log.setOperatorId(operator);
            log.setReason(reason);
            log.setCreatedAt(Instant.now());
            execMapper.insert(log);
        } catch (Exception ignored) {
            // 日志失败不影响主流程
        }
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "DISPATCH", code, status, null, reason));

        return Map.of(
                "executionId", executionId,
                "commandCode", code,
                "status", status,
                "result", result == null ? Map.of() : result,
                "gapHint", gapHint == null ? "" : gapHint,
                "durationMs", duration
        );
    }
}
