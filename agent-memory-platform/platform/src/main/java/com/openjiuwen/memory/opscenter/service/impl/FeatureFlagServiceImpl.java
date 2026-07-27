package com.openjiuwen.memory.opscenter.service.impl;

import com.openjiuwen.memory.common.client.dto.AddMessagesRequest;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.opscenter.domain.FeatureFlagEntity;
import com.openjiuwen.memory.opscenter.mapper.FeatureFlagMapper;
import com.openjiuwen.memory.opscenter.service.FeatureFlagService;
import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.List;

@Service
public class FeatureFlagServiceImpl implements FeatureFlagService {

    private static final String DEFAULT_TENANT = "default";

    private final FeatureFlagMapper mapper;
    private final PermissionChecker permissionChecker;
    private final AuditRecorder auditRecorder;

    @Value("${platform.feature.default-scope:__default__}")
    private String defaultScope;

    public FeatureFlagServiceImpl(FeatureFlagMapper mapper,
                                  PermissionChecker permissionChecker,
                                  AuditRecorder auditRecorder) {
        this.mapper = mapper;
        this.permissionChecker = permissionChecker;
        this.auditRecorder = auditRecorder;
    }

    @Override
    public List<FeatureFlagEntity> list() {
        permissionChecker.check("config:read");
        return mapper.selectList(null);
    }

    @Override
    public AddMessagesRequest resolve(String scopeId) {
        FeatureFlagEntity scopeProfile = mapper.findByTenantIdAndScopeId(DEFAULT_TENANT, scopeId);
        FeatureFlagEntity defaultProfile = mapper.findByTenantIdAndScopeId(DEFAULT_TENANT, defaultScope);
        AddMessagesRequest req = new AddMessagesRequest();
        req.setEnableLongTermMem(pick(scopeProfile, defaultProfile, FeatureFlagEntity::getEnableLongTermMem, true));
        req.setEnableUserProfile(pick(scopeProfile, defaultProfile, FeatureFlagEntity::getEnableUserProfile, true));
        req.setEnableSemanticMemory(pick(scopeProfile, defaultProfile, FeatureFlagEntity::getEnableSemanticMemory, true));
        req.setEnableEpisodicMemory(pick(scopeProfile, defaultProfile, FeatureFlagEntity::getEnableEpisodicMemory, true));
        req.setEnableSummaryMemory(pick(scopeProfile, defaultProfile, FeatureFlagEntity::getEnableSummaryMemory, true));
        return req;
    }

    @Override
    public FeatureView get(String scopeId) {
        permissionChecker.check("config:read");
        FeatureFlagEntity scopeProfile = mapper.findByTenantIdAndScopeId(DEFAULT_TENANT, scopeId);
        String inheritedFrom = (scopeProfile == null) ? defaultScope : scopeId;
        return new FeatureView(toFlags(scopeProfile), resolve(scopeId), inheritedFrom);
    }

    @Override
    public void upsert(String scopeId, AddMessagesRequest flags, String operator) {
        permissionChecker.check("config:write");
        FeatureFlagEntity e = mapper.findByTenantIdAndScopeId(DEFAULT_TENANT, scopeId);
        boolean isNew = (e == null);
        if (isNew) {
            e = new FeatureFlagEntity();
            e.setTenantId(DEFAULT_TENANT);
            e.setScopeId(scopeId);
            e.setCreatedAt(Instant.now());
        }
        if (flags.getEnableLongTermMem() != null) e.setEnableLongTermMem(flags.getEnableLongTermMem());
        if (flags.getEnableUserProfile() != null) e.setEnableUserProfile(flags.getEnableUserProfile());
        if (flags.getEnableSemanticMemory() != null) e.setEnableSemanticMemory(flags.getEnableSemanticMemory());
        if (flags.getEnableEpisodicMemory() != null) e.setEnableEpisodicMemory(flags.getEnableEpisodicMemory());
        if (flags.getEnableSummaryMemory() != null) e.setEnableSummaryMemory(flags.getEnableSummaryMemory());
        e.setEnabled(true);
        e.setUpdatedAt(Instant.now());
        e.setUpdatedBy(operator);
        if (isNew) {
            mapper.insert(e);
        } else {
            mapper.updateById(e);
        }
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "PUT", "/ops/features/" + scopeId, "success", null, null));
    }

    @Override
    public void toggle(String scopeId, String flag, boolean value, String operator) {
        permissionChecker.check("config:write");
        FeatureFlagEntity e = mapper.findByTenantIdAndScopeId(DEFAULT_TENANT, scopeId);
        if (e == null) {
            throw new BizException(ResultCode.NOT_FOUND, "scope 特性配置不存在: " + scopeId);
        }
        switch (flag) {
            case "enableLongTermMem" -> e.setEnableLongTermMem(value);
            case "enableUserProfile" -> e.setEnableUserProfile(value);
            case "enableSemanticMemory" -> e.setEnableSemanticMemory(value);
            case "enableEpisodicMemory" -> e.setEnableEpisodicMemory(value);
            case "enableSummaryMemory" -> e.setEnableSummaryMemory(value);
            default -> throw new BizException(ResultCode.BAD_REQUEST, "未知特性开关: " + flag);
        }
        e.setUpdatedAt(Instant.now());
        e.setUpdatedBy(operator);
        mapper.updateById(e);
    }

    @Override
    public void delete(String scopeId, String operator) {
        permissionChecker.check("config:write");
        if (defaultScope.equals(scopeId)) {
            throw new BizException(ResultCode.BAD_REQUEST, "默认 profile 不可删除");
        }
        FeatureFlagEntity e = mapper.findByTenantIdAndScopeId(DEFAULT_TENANT, scopeId);
        if (e != null) {
            mapper.deleteById(e.getId());
        }
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "DELETE", "/ops/features/" + scopeId, "success", null, null));
    }

    // —— 内部 ——

    private boolean pick(FeatureFlagEntity scope, FeatureFlagEntity def,
                         java.util.function.Function<FeatureFlagEntity, Boolean> getter, boolean fallback) {
        if (scope != null && Boolean.TRUE.equals(scope.getEnabled()) && getter.apply(scope) != null) {
            return getter.apply(scope);
        }
        if (def != null && getter.apply(def) != null) {
            return getter.apply(def);
        }
        return fallback;
    }

    private AddMessagesRequest toFlags(FeatureFlagEntity e) {
        if (e == null) return null;
        AddMessagesRequest r = new AddMessagesRequest();
        r.setEnableLongTermMem(e.getEnableLongTermMem());
        r.setEnableUserProfile(e.getEnableUserProfile());
        r.setEnableSemanticMemory(e.getEnableSemanticMemory());
        r.setEnableEpisodicMemory(e.getEnableEpisodicMemory());
        r.setEnableSummaryMemory(e.getEnableSummaryMemory());
        return r;
    }
}
