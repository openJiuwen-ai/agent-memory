package com.openjiuwen.memory.configcenter.service.impl;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.configcenter.dto.TenantScopeConfigListItemDTO;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.configcenter.domain.ConfigAuditLogEntity;
import com.openjiuwen.memory.configcenter.domain.ConfigTemplateEntity;
import com.openjiuwen.memory.configcenter.domain.TenantScopeConfigEntity;
import com.openjiuwen.memory.configcenter.dto.TenantScopeConfigDTO;
import com.openjiuwen.memory.configcenter.mapper.ConfigAuditLogMapper;
import com.openjiuwen.memory.configcenter.mapper.ConfigTemplateMapper;
import com.openjiuwen.memory.configcenter.mapper.TenantScopeConfigMapper;
import com.openjiuwen.memory.configcenter.service.TenantScopeConfigService;
import com.openjiuwen.memory.scopecenter.domain.ScopeRegistry;
import com.openjiuwen.memory.scopecenter.service.ScopeRegistryService;
import com.openjiuwen.memory.tenantcenter.domain.Tenant;
import com.openjiuwen.memory.tenantcenter.mapper.TenantMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * 租户级 Scope 配置服务实现 — 2026-07-17 P0-3 v2 重构
 */
@Service
public class TenantScopeConfigServiceImpl implements TenantScopeConfigService {

    private static final Logger log = LoggerFactory.getLogger(TenantScopeConfigServiceImpl.class);

    private final TenantScopeConfigMapper tenantScopeConfigMapper;
    private final ConfigTemplateMapper templateMapper;
    private final ConfigAuditLogMapper auditLogMapper;
    private final MemoryEngineClient memoryEngineClient;
    private final ScopeRegistryService scopeRegistryService;
    private final TenantMapper tenantMapper;
    private final ObjectMapper objectMapper;

    public TenantScopeConfigServiceImpl(TenantScopeConfigMapper tenantScopeConfigMapper,
                                         ConfigTemplateMapper templateMapper,
                                         ConfigAuditLogMapper auditLogMapper,
                                         MemoryEngineClient memoryEngineClient,
                                         ScopeRegistryService scopeRegistryService,
                                         TenantMapper tenantMapper,
                                         ObjectMapper objectMapper) {
        this.tenantScopeConfigMapper = tenantScopeConfigMapper;
        this.templateMapper = templateMapper;
        this.auditLogMapper = auditLogMapper;
        this.memoryEngineClient = memoryEngineClient;
        this.scopeRegistryService = scopeRegistryService;
        this.tenantMapper = tenantMapper;
        this.objectMapper = objectMapper;
    }

    @Override
    public TenantScopeConfigDTO getByTenant(String tenantId) {
        TenantScopeConfigEntity entity = tenantScopeConfigMapper.findByTenantId(tenantId);
        if (entity == null) {
            throw new BizException(ResultCode.NOT_FOUND, "租户配置不存在: " + tenantId);
        }
        return toDTO(entity);
    }

    @Override
    @Transactional
    public TenantScopeConfigDTO update(String tenantId, String configJson, String operator) {
        TenantScopeConfigEntity entity = tenantScopeConfigMapper.findByTenantId(tenantId);
        if (entity == null) {
            throw new BizException(ResultCode.NOT_FOUND, "租户配置不存在: " + tenantId);
        }
        String before = entity.getConfigJson();
        entity.setConfigJson(configJson);
        entity.setCurrentVersion(entity.getCurrentVersion() == null ? 1 : entity.getCurrentVersion() + 1);
        entity.setUpdatedAt(Instant.now());
        entity.setUpdatedBy(operator);
        tenantScopeConfigMapper.updateById(entity);
        memoryEngineClient.setScopeConfig(resolveScopeId(tenantId), parseScopeConfigJson(configJson));
        recordAudit(operator, tenantId, "TENANT_CONFIG_UPDATE", before, configJson, true, null, null);
        return toDTO(entity);
    }

    @Override
    public void delete(String tenantId, String operator) {
        // 实际不允许删除（租户是身份）
        throw new BizException(ResultCode.FORBIDDEN, "租户配置不能单独删除（请走租户删除流程）");
    }

    @Override
    public List<TenantScopeConfigListItemDTO> listAll() {
        return tenantScopeConfigMapper.listAll().stream().map(this::toListItemDTO).toList();
    }

    @Override
    public List<TenantScopeConfigListItemDTO> listAll(String templateId) {
        if (templateId == null || templateId.isBlank()) {
            throw new BizException(ResultCode.BAD_REQUEST, "templateId 不能为空（列表必须按模板过滤）");
        }
        // JOIN scope_registry 一次带出 scope_id（绑定关系权威来源是 scope_registry，见 TenantController.create）
        return tenantScopeConfigMapper.listByTemplateIdWithScope(templateId).stream().map(this::toListItemDTO).toList();
    }

    @Override
    public List<TenantScopeConfigListItemDTO> listDeviated() {
        return tenantScopeConfigMapper.selectList(
            new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<TenantScopeConfigEntity>()
                .apply("template_version IS NOT NULL AND current_version != template_version"))
            .stream().map(this::toListItemDTO).toList();
    }

    @Override
    @Transactional
    public TenantScopeConfigDTO syncFromTemplate(String tenantId, String operator) {
        TenantScopeConfigEntity entity = tenantScopeConfigMapper.findByTenantId(tenantId);
        if (entity == null) {
            throw new BizException(ResultCode.NOT_FOUND, "租户配置不存在: " + tenantId);
        }
        if (entity.getTemplateId() == null) {
            throw new BizException(ResultCode.BAD_REQUEST, "租户未应用任何模板，无法同步回模板");
        }
        ConfigTemplateEntity template = templateMapper.selectById(entity.getTemplateId());
        if (template == null) {
            throw new BizException(ResultCode.NOT_FOUND, "关联模板已删除");
        }
        String before = entity.getConfigJson();
        entity.setConfigJson(template.getConfigJson());
        entity.setTemplateVersion(template.getVersion());
        entity.setCurrentVersion(template.getVersion());
        entity.setUpdatedAt(Instant.now());
        entity.setUpdatedBy(operator);
        tenantScopeConfigMapper.updateById(entity);
        memoryEngineClient.setScopeConfig(resolveScopeId(tenantId), parseScopeConfigJson(template.getConfigJson()));
        recordAudit(operator, tenantId, "SYNC_FROM_TEMPLATE", before, template.getConfigJson(),
            true, null, "平台操作: 重新下发模板");
        return toDTO(entity);
    }

    /**
     * 列表视图专用：物理不含 config_json（由 SQL 层 LIST_COLUMNS 剔除，DTO 也不声明该字段）。
     * scopeIds 从 JSON 数组解析（如 ["scope_02"]）；租户未绑定 scope 时给空列表，不回退 tenant_id。
     */
    private static List<String> parseScopeIds(String scopeIdsJson) {
        if (scopeIdsJson == null || scopeIdsJson.isBlank()) {
            return List.of();
        }
        try {
            // 尝试解析 JSON 数组格式，如 ["scope_02"]
            if (scopeIdsJson.trim().startsWith("[")) {
                ObjectMapper mapper = new ObjectMapper();
                List<String> list = mapper.readValue(scopeIdsJson, new TypeReference<List<String>>() {});
                return list;
            }
            // 回退：逗号分隔的旧格式
            return List.of(scopeIdsJson.split(","));
        } catch (Exception e) {
            // 解析失败时返回空列表
            return List.of();
        }
    }

    private TenantScopeConfigListItemDTO toListItemDTO(TenantScopeConfigEntity e) {
        TenantScopeConfigListItemDTO dto = new TenantScopeConfigListItemDTO();
        dto.setTenantId(e.getTenantId());
        dto.setTenantName(e.getTenantName());
        // GROUP_CONCAT 逗号串拆 List；空值（租户未绑定 scope）给空列表，不回退 tenant_id（避免误导）
        dto.setScopeIds(parseScopeIds(e.getScopeId()));
        dto.setInstanceId(e.getInstanceId());
        dto.setTemplateId(e.getTemplateId());
        dto.setTemplateVersion(e.getTemplateVersion());
        dto.setCurrentVersion(e.getCurrentVersion());
        dto.setIsDeviated(e.getTemplateVersion() != null && e.getCurrentVersion() != null
            && !e.getTemplateVersion().equals(e.getCurrentVersion()));
        dto.setUpdatedAt(e.getUpdatedAt() != null ? e.getUpdatedAt().toString() : null);
        dto.setUpdatedBy(e.getUpdatedBy());
        if (e.getTemplateId() != null) {
            ConfigTemplateEntity t = templateMapper.selectById(e.getTemplateId());
            if (t != null) {
                dto.setTemplateName(t.getDisplayName());
            }
        }
        return dto;
    }

    private TenantScopeConfigDTO toDTO(TenantScopeConfigEntity e) {
        TenantScopeConfigDTO dto = new TenantScopeConfigDTO();
        dto.setTenantId(e.getTenantId());
        dto.setTenantName(e.getTenantName());
        // scope_id 权威来源 = scope_registry（assigned_to_tenant_id + status='assigned'）。
        // 单条接口只查 1 个租户，这里直查一次（无 N+1）；未绑定给 null，不回退 tenant_id（避免误导）。
        List<ScopeRegistry> bindings = scopeRegistryService.getScopesByTenantId(e.getTenantId());
        dto.setScopeId(bindings == null || bindings.isEmpty() ? null : bindings.get(0).getScopeId());
        dto.setInstanceId(e.getInstanceId());
        dto.setConfigJson(e.getConfigJson());
        dto.setTemplateId(e.getTemplateId());
        dto.setTemplateVersion(e.getTemplateVersion());
        dto.setCurrentVersion(e.getCurrentVersion());
        dto.setIsDeviated(e.getTemplateVersion() != null && e.getCurrentVersion() != null
            && !e.getTemplateVersion().equals(e.getCurrentVersion()));
        dto.setUpdatedAt(e.getUpdatedAt() != null ? e.getUpdatedAt().toString() : null);
        dto.setUpdatedBy(e.getUpdatedBy());
        if (e.getTemplateId() != null) {
            ConfigTemplateEntity t = templateMapper.selectById(e.getTemplateId());
            if (t != null) {
                dto.setTemplateName(t.getDisplayName());
            }
        }
        return dto;
    }

    private void recordAudit(String operator, String tenantId, String operation,
                              String before, String after, boolean success,
                              String errorMsg, String reason) {
        ConfigAuditLogEntity audit = new ConfigAuditLogEntity();
        audit.setId(UUID.randomUUID().toString());
        audit.setOperatorId(operator);
        audit.setTenantId(tenantId);
        audit.setInstanceId("default");
        audit.setOperation(operation);
        audit.setBeforeValue(before);
        audit.setAfterValue(after);
        audit.setSuccess(success);
        audit.setErrorMessage(errorMsg);
        audit.setOperatedAt(Instant.now());
        audit.setReason(reason);
        auditLogMapper.insert(audit);
    }

    private String resolveScopeId(String tenantId) {
        // 从 tenants 表读取 scope_ids（权威来源）
        Tenant tenant = tenantMapper.selectById(tenantId);
        if (tenant == null) {
            throw new BizException(ResultCode.NOT_FOUND, "租户不存在: " + tenantId);
        }
        List<String> scopeIds = parseScopeIds(tenant.getScopeIds());
        if (scopeIds == null || scopeIds.isEmpty()) {
            throw new BizException(ResultCode.BAD_REQUEST, "租户未绑定 scope_id，无法下发 Scope 配置");
        }
        if (scopeIds.size() > 1) {
            throw new BizException(ResultCode.BAD_REQUEST, "租户绑定了多个 scope_id，当前配置中心仅支持一对一映射");
        }
        return scopeIds.get(0);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> parseScopeConfigJson(String configJson) {
        try {
            Map<String, Object> config = objectMapper.readValue(configJson, Map.class);
            // 自动填充空的 API_KEY 从环境变量
            fillEmptyApiKeysFromEnv(config);
            return config;
        } catch (Exception e) {
            throw new BizException(ResultCode.BAD_REQUEST, "租户 Scope 配置不是合法 JSON: " + e.getMessage());
        }
    }

    /**
     * 当配置中的 API_KEY 为空时，从环境变量或内核 .env 文件自动填充
     */
    @SuppressWarnings("unchecked")
    private void fillEmptyApiKeysFromEnv(Map<String, Object> config) {
        // 不再自动填充 API Key，由用户手动输入
    }

}
