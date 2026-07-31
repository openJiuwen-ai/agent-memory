package com.openjiuwen.memory.opscenter.service.impl;

import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.dto.AddMessagesRequest;
import com.openjiuwen.memory.common.client.dto.DeleteByScopeRequest;
import com.openjiuwen.memory.common.client.dto.DeleteVariablesRequest;
import com.openjiuwen.memory.common.client.dto.GetVariablesRequest;
import com.openjiuwen.memory.common.client.dto.GetUserMemByPageRequest;
import com.openjiuwen.memory.common.client.dto.MemVariable;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.client.dto.UpdateMemoryRequest;
import com.openjiuwen.memory.common.client.dto.UpdateVariablesRequest;
import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.authcenter.domain.User;
import com.openjiuwen.memory.authcenter.mapper.UserMapper;
import com.openjiuwen.memory.opscenter.domain.MemoryChangeLogSnapshotEntity;
import com.openjiuwen.memory.opscenter.mapper.MemoryChangeLogSnapshotMapper;
import com.openjiuwen.memory.opscenter.service.FeatureFlagService;
import com.openjiuwen.memory.opscenter.service.MemoryManageService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Service
public class MemoryManageServiceImpl implements MemoryManageService {

    private static final Logger log = LoggerFactory.getLogger(MemoryManageServiceImpl.class);

    private final MemoryEngineClient client;
    private final FeatureFlagService featureFlagService;
    private final MemoryChangeLogSnapshotMapper changeLogMapper;
    private final PermissionChecker permissionChecker;
    private final AuditRecorder auditRecorder;
    private final TenantContextProvider tenantContextProvider;
    private final ObjectMapper objectMapper;
    private final UserMapper userMapper;

    public MemoryManageServiceImpl(MemoryEngineClient client,
                                   FeatureFlagService featureFlagService,
                                   MemoryChangeLogSnapshotMapper changeLogMapper,
                                   PermissionChecker permissionChecker,
                                   AuditRecorder auditRecorder,
                                   TenantContextProvider tenantContextProvider,
                                   ObjectMapper objectMapper,
                                   UserMapper userMapper) {
        this.client = client;
        this.featureFlagService = featureFlagService;
        this.changeLogMapper = changeLogMapper;
        this.permissionChecker = permissionChecker;
        this.auditRecorder = auditRecorder;
        this.tenantContextProvider = tenantContextProvider;
        this.objectMapper = objectMapper;
        this.userMapper = userMapper;
    }

    @Override
    public PageResult<MemoryItem> list(String userId, String scopeId, String memoryType, int pageIdx, int pageSize) {
        permissionChecker.check("memory:read");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);

        // scopeId 为空时降级到 __default__（listScopes 未实现，listAcrossAllScopes 最终也降级到 __default__）
        String effScope = (scopeId == null || scopeId.isBlank()) ? "__default__" : scopeId;

        GetUserMemByPageRequest req = new GetUserMemByPageRequest();
        req.setUserId(userId);
        req.setScopeId(effScope);
        // memoryType 为空时不传 null（:8516 的 MemoryType(null.lower()) 会崩），保留 DTO 默认 "unknown"
        if (memoryType != null && !memoryType.isBlank()) {
            req.setMemoryType(memoryType);
        }
        req.setPageIdx(pageIdx);
        req.setPageSize(pageSize);
        PageResult<MemoryItem> page = client.getUserMemByPage(req);
        // :8516 get_user_mem_by_page 不返 user_id/scope_id，按查询条件回显（与 client null→__default__ 规范一致）
        String effUser = (userId == null || userId.isBlank()) ? "__default__" : userId;
        if (page.items() != null) {
            for (MemoryItem item : page.items()) {
                item.setUserId(effUser);
                item.setScopeId(effScope);
            }
        }
        return page;
    }
    
    @Override
    public MemoryItem detail(String memId, String userId, String scopeId) {
        permissionChecker.check("memory:read");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        // :8516 无 get_mem_by_id；多页翻页定位（降级，待记忆服务补端点）
        int pageSize = 100;
        int maxPages = 10;
        int pageIdx = 1;
        while (pageIdx <= maxPages) {
            GetUserMemByPageRequest req = new GetUserMemByPageRequest();
            req.setUserId(userId);
            req.setScopeId(scopeId);
            req.setPageSize(pageSize);
            req.setPageIdx(pageIdx);
            PageResult<MemoryItem> page = client.getUserMemByPage(req);
            if (page == null || page.items() == null || page.items().isEmpty()) {
                break;
            }
            MemoryItem found = page.items().stream()
                    .filter(m -> memId.equals(m.getMemId()))
                    .findFirst()
                    .orElse(null);
            if (found != null) return found;
            if (page.items().size() < pageSize) break;
            pageIdx++;
        }
        throw new BizException(ResultCode.NOT_FOUND, "记忆不存在或不在该 user/scope 下: " + memId);
    }

    @Override
    public Object create(String userId, String scopeId, List<Map<String, String>> messages,
                         List<MemVariable> memVariables, String operator, String reason) {
        permissionChecker.check("memory:write");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        // enable_* 由特性配置注入，不由调用方传
        AddMessagesRequest req = featureFlagService.resolve(scopeId);
        req.setUserId(userId);
        req.setScopeId(scopeId);
        req.setMessages(messages);
        // memVariables 为 null 时归一为空列表，避免 Jackson 把 null 序列化成
        // "mem_variables": null，被 :8516 的 Pydantic（list[MemVariable]）判 422。
        req.setMemVariables(memVariables != null ? memVariables : Collections.emptyList());
        var result = client.addMessages(req);
        // :8516 add_messages 不返回 mem_id，CREATE 快照 memId=null。
        // 方案 C：将 user_id + scope_id + messages 序列化到 newContent，供未来按 user/scope 追溯 CREATE 记录。
        Map<String, Object> snapshotContent = new LinkedHashMap<>();
        snapshotContent.put("user_id", userId);
        snapshotContent.put("scope_id", scopeId);
        snapshotContent.put("messages", messages);
        if (memVariables != null && !memVariables.isEmpty()) {
            snapshotContent.put("mem_variables", memVariables);
        }
        String newContentJson;
        try {
            newContentJson = objectMapper.writeValueAsString(snapshotContent);
        } catch (Exception e) {
            newContentJson = null;
        }
        saveSnapshot(null, "CREATE", null, newContentJson, operator, reason);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "POST", "/ops/memory", "success", null, reason));
        return result;
    }

    @Override
    public Object update(String memId, String memory, String oldContent, String userId, String scopeId, String operator, String reason) {
        permissionChecker.check("memory:write");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        // 前端传入旧 content，无需翻页查找
        saveSnapshot(memId, "UPDATE", oldContent, memory, operator, reason);

        UpdateMemoryRequest req = new UpdateMemoryRequest();
        req.setMemId(memId);
        req.setMemory(memory);
        req.setUserId(userId);
        req.setScopeId(scopeId);
        var result = client.updateMemById(req);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "PUT", "/ops/memory/" + memId, "success", null, reason));
        return result;
    }

    @Override
    public Object deleteOne(String memId, String userId, String scopeId, String oldContent, String operator, String reason) {
        permissionChecker.check("memory:delete");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        // 前端传入旧 content，无需翻页查找
        saveSnapshot(memId, "DELETE", oldContent, null, operator, reason);
        var result = client.deleteMemById(memId, userId, scopeId);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "DELETE", "/ops/memory/" + memId, "success", null, reason));
        return result;
    }

    @Override
    public Object deleteByScope(String scopeId, String confirmToken, String operator, String reason) {
        permissionChecker.check("memory:delete");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        // confirmToken 由 OpsToolService.purgeScopePreview 签发；此处校验（简化：非空即放行，待接入 TokenService）
        if (confirmToken == null || confirmToken.isBlank()) {
            throw new BizException(ResultCode.CONFIRM_TOKEN_INVALID, "清空 scope 需 confirmToken 二次确认");
        }
        saveSnapshot(null, "DELETE", null, null, operator, reason);
        DeleteByScopeRequest req = new DeleteByScopeRequest();
        req.setScopeId(scopeId);
        var result = client.deleteMemByScope(req);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "DELETE", "/ops/memory?scopeId=" + scopeId, "success", null, reason));
        return result;
    }

    @Override
    public Object batchDelete(List<String> memIds, String userId, String scopeId, String operator, String reason) {
        permissionChecker.check("memory:delete");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        if (memIds == null || memIds.isEmpty()) {
            throw new BizException(ResultCode.BAD_REQUEST, "mem_ids 不能为空");
        }
        // 逐条保存 DELETE 快照（oldContent 不逐条获取，避免 N 次翻页查询；memId 已知，未来追溯入口可按 memId 查）
        for (String memId : memIds) {
            saveSnapshot(memId, "DELETE", null, null, operator, reason);
        }
        var result = client.batchDeleteMem(memIds, userId, scopeId);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "POST", "/ops/memory/batch-delete", "success", null, reason));
        return result;
    }

    @Override
    public Map<String, String> getVariables(String userId, String scopeId, List<String> names) {
        permissionChecker.check("memory:read");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        GetVariablesRequest req = new GetVariablesRequest();
        req.setUserId(userId);
        req.setScopeId(scopeId);
        req.setNames(names);
        return client.getVariables(req);
    }

    @Override
    public Object updateVariables(String userId, String scopeId, Map<String, String> variables, String operator) {
        permissionChecker.check("memory:write");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        UpdateVariablesRequest req = new UpdateVariablesRequest();
        req.setUserId(userId);
        req.setScopeId(scopeId);
        req.setVariables(variables);
        var result = client.updateVariables(req);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "PUT", "/ops/memory/variables", "success", null, null));
        return result;
    }

    @Override
    public Object deleteVariables(String userId, String scopeId, List<String> names, String operator) {
        permissionChecker.check("memory:delete");
        // V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
        validateScopeAccess(scopeId);
        DeleteVariablesRequest req = new DeleteVariablesRequest();
        req.setUserId(userId);
        req.setScopeId(scopeId);
        req.setNames(names);
        var result = client.deleteVariables(req);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "DELETE", "/ops/memory/variables", "success", null, null));
        return result;
    }

    /**
     * V3-DEFECT-035 修复：校验租户级角色的 Scope 访问权限
     * <p>
     * 平台级角色（SUPER_ADMIN/PLATFORM_ADMIN/SECURITY_ADMIN）可访问所有 Scope；
     * 租户级角色（SCOPE_ADMIN/READ_ONLY/VIEWER）只能访问自己绑定的 Scope。
     *
     * @param scopeId 请求访问的 Scope ID
     * @throws BizException 当租户级角色尝试访问未绑定的 Scope 时抛出
     */
    private void validateScopeAccess(String scopeId) {
        // scopeId 为空或默认值时跳过校验
        if (scopeId == null || scopeId.isBlank() || "__default__".equals(scopeId)) {
            return;
        }

        // 获取当前用户上下文
        TenantContextProvider.TenantContext ctx = tenantContextProvider.current();
        if (ctx == null || ctx.userId() == null) {
            return;
        }

        String role = ctx.role();
        // 平台级角色直接放行
        if ("SUPER_ADMIN".equals(role) || "PLATFORM_ADMIN".equals(role) || "SECURITY_ADMIN".equals(role)) {
            return;
        }

        // 租户级角色需要校验 Scope 绑定关系
        User user = userMapper.selectById(ctx.userId());
        if (user == null || user.getScopeIds() == null || user.getScopeIds().isBlank()) {
            throw new BizException(ResultCode.FORBIDDEN, "当前用户未绑定任何 Scope，无权访问：" + scopeId);
        }

        // 解析 scopeIds（JSON 数组格式，如 ["scope_01","scope_02"]）
        String scopeIdsStr = user.getScopeIds().trim();
        if (scopeIdsStr.startsWith("[") && scopeIdsStr.endsWith("]")) {
            scopeIdsStr = scopeIdsStr.substring(1, scopeIdsStr.length() - 1);
        }
        List<String> boundScopeIds = Arrays.asList(scopeIdsStr.split(","));
        boolean hasAccess = boundScopeIds.stream()
                .map(String::trim)
                .map(s -> s.replace("\"", ""))
                .anyMatch(s -> s.equals(scopeId));

        if (!hasAccess) {
            throw new BizException(ResultCode.FORBIDDEN,
                    String.format("当前角色 %s 无权访问 Scope：%s（仅可访问已绑定的 Scope）", role, scopeId));
        }
    }

    private void saveSnapshot(String memId, String type, String oldContent, String newContent, String operator, String reason) {
        MemoryChangeLogSnapshotEntity e = new MemoryChangeLogSnapshotEntity();
        e.setMemId(memId);
        // Fix #14: 从 TenantContextProvider 获取租户，不再硬编码 "default"
        TenantContextProvider.TenantContext ctx = tenantContextProvider.current();
        e.setTenantId(ctx != null ? ctx.tenantId() : "default");
        e.setChangeType(type);
        e.setOldContent(oldContent);
        e.setNewContent(newContent);
        e.setOperatorId(operator);
        e.setReason(reason);
        e.setCreatedAt(Instant.now());
        changeLogMapper.insert(e);
    }
}
