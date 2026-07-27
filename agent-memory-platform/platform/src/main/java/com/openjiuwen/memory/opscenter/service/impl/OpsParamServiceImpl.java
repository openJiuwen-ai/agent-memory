package com.openjiuwen.memory.opscenter.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.common.exception.GapException;
import com.openjiuwen.memory.opscenter.domain.OpsParameterEntity;
import com.openjiuwen.memory.opscenter.mapper.OpsParameterMapper;
import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.common.spi.ConfigCenterClient;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.opscenter.service.OpsParamService;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Service
public class OpsParamServiceImpl implements OpsParamService {

    private static final String DEFAULT_TENANT = "default";

    private final ConfigCenterClient configCenter;
    private final OpsParameterMapper paramMapper;
    private final PermissionChecker permissionChecker;
    private final AuditRecorder auditRecorder;
    private final ObjectMapper objectMapper;

    public OpsParamServiceImpl(ConfigCenterClient configCenter,
                               OpsParameterMapper paramMapper,
                               PermissionChecker permissionChecker,
                               AuditRecorder auditRecorder,
                               ObjectMapper objectMapper) {
        this.configCenter = configCenter;
        this.paramMapper = paramMapper;
        this.permissionChecker = permissionChecker;
        this.auditRecorder = auditRecorder;
        this.objectMapper = objectMapper;
    }

    @Override
    public Map<String, Object> overview() {
        permissionChecker.check("config:read");
        boolean available;
        try {
            configCenter.getEngineConfig();
            available = true;
        } catch (GapException e) {
            available = false;
        }
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("categories", List.of(
                Map.of("key", "retrieval", "available", available),
                Map.of("key", "engine", "available", available),
                Map.of("key", "scope", "available", available),
                Map.of("key", "agent", "available", available),
                Map.of("key", "dreaming", "available", available)
        ));
        data.put("configCenterAvailable", available);
        return data;
    }

    @Override
    public Map<String, Object> get(String category, String scopeId) {
        permissionChecker.check("config:read");
        Map<String, Object> data = new LinkedHashMap<>();
        Object effective = defaultEffective(category);
        Object config = null;
        boolean available;
        try {
            config = readFromConfigCenter(category, scopeId);
            available = true;
        } catch (GapException e) {
            available = false;
        }
        data.put("available", available);
        data.put("config", config);
        data.put("effective", effective);
        data.put("hint", available ? "" : "配置中心未接入；effective 为本模块调用 :8516 时使用的默认值");
        return data;
    }

    @Override
    public Map<String, Object> update(String category, String scopeId, Map<String, Object> value, String operator) {
        permissionChecker.check("config:write");
        // 先存本地草稿（即使写回失败也保留）
        saveDraft(category, scopeId, value, operator);
        try {
            writeToConfigCenter(category, scopeId, value);
            auditRecorder.record(new AuditRecorder.AuditEvent(operator, "PUT", "/ops/params/" + category, "success", null, null));
            return Map.of("draftSaved", true, "writtenBack", true);
        } catch (GapException e) {
            return Map.of("draftSaved", true, "writtenBack", false, "gapHint", e.gapHint());
        }
    }

    @Override
    public void saveDraft(String category, String scopeId, Map<String, Object> value, String operator) {
        permissionChecker.check("config:write");
        OpsParameterEntity e = new OpsParameterEntity();
        e.setTenantId(DEFAULT_TENANT);
        e.setScopeId(scopeId);
        e.setParamKey(category);
        e.setParamType(category);
        try {
            e.setValueJson(objectMapper.writeValueAsString(value == null ? Map.of() : value));
        } catch (Exception ex) {
            e.setValueJson("{}");
        }
        e.setIsDraft(true);
        e.setUpdatedAt(Instant.now());
        e.setUpdatedBy(operator);
        paramMapper.upsertDraft(e);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "POST", "/ops/params/draft/save", "success", null, category));
    }

    // —— 配置中心读写（未接入抛 GapException） ——
    private Object readFromConfigCenter(String category, String scopeId) {
        return switch (category) {
            case "engine", "retrieval" -> configCenter.getEngineConfig();
            case "scope" -> configCenter.getScopeConfig(scopeId);
            case "agent" -> configCenter.getAgentConfig(scopeId);
            default -> throw new GapException("配置中心未接入，" + category + " 参数只读");
        };
    }

    private void writeToConfigCenter(String category, String scopeId, Map<String, Object> value) {
        switch (category) {
            case "engine", "retrieval" -> configCenter.updateEngineConfig(value);
            case "scope" -> configCenter.updateScopeConfig(scopeId, value);
            case "agent" -> configCenter.updateAgentConfig(scopeId, value);
            default -> throw new GapException("配置中心未接入，无法写回 " + category + " 参数");
        }
    }

    /** 本模块调用 :8516 时使用的默认值（如检索 topK/threshold） */
    private Object defaultEffective(String category) {
        if ("retrieval".equals(category)) {
            return Map.of("defaultTopK", 10, "defaultThreshold", 0.3, "searchTimeoutMs", 5000);
        }
        return null;
    }
}
