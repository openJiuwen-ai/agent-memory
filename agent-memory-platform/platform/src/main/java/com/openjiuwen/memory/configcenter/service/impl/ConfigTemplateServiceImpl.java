package com.openjiuwen.memory.configcenter.service.impl;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.spi.ConfirmTokenService;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.configcenter.domain.ConfigAuditLogEntity;
import com.openjiuwen.memory.configcenter.domain.ConfigTemplateEntity;
import com.openjiuwen.memory.configcenter.domain.InstanceConfigEntity;
import com.openjiuwen.memory.configcenter.domain.TenantScopeConfigEntity;
import com.openjiuwen.memory.configcenter.dto.ApplyTemplateRequest;
import com.openjiuwen.memory.configcenter.dto.ConfigTemplateListItemDTO;
import com.openjiuwen.memory.configcenter.dto.CreateTemplateRequest;
import com.openjiuwen.memory.configcenter.dto.TemplateApplyResultDTO;
import com.openjiuwen.memory.configcenter.dto.TemplateDeleteResultDTO;
import com.openjiuwen.memory.configcenter.dto.TemplateTenantUsageDTO;
import com.openjiuwen.memory.configcenter.dto.UpdateTemplateRequest;
import com.openjiuwen.memory.configcenter.mapper.ConfigAuditLogMapper;
import com.openjiuwen.memory.configcenter.mapper.ConfigTemplateMapper;
import com.openjiuwen.memory.configcenter.mapper.InstanceConfigMapper;
import com.openjiuwen.memory.configcenter.mapper.TenantScopeConfigMapper;
import com.openjiuwen.memory.configcenter.service.ConfigTemplateService;
import com.openjiuwen.memory.scopecenter.domain.ScopeRegistry;
import com.openjiuwen.memory.scopecenter.service.ScopeRegistryService;
import com.openjiuwen.memory.tenantcenter.domain.Tenant;
import com.openjiuwen.memory.tenantcenter.mapper.TenantMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.stream.Collectors;

/**
 * 模板服务实现 — 2026-07-19 P0-3 v3 重构
 * <p>
 * 简化为 2 种类型 SCOPE / INSTANCE。
 * <p>
 * INSTANCE 模板分为两类预置模板：
 * <ul>
 *   <li>热启动模板（tpl_instance_hot）：修改后立即生效，无需重启</li>
 *   <li>冷启动模板（tpl_instance_cold）：修改后需重启引擎才能生效，应用时强制触发重启</li>
 * </ul>
 * <p>
 * 冷启动模板应用/更新时强制引擎重启：
 * <ul>
 *   <li>要求操作人具备 kernel:restart 权限</li>
 *   <li>必须携带 confirm_token（由 /api/v1/config/kernel/confirm-token 签发，ACTION_KERNEL_RESTART）</li>
 *   <li>Push 到内核成功后调用 {@link MemoryEngineClient#restartKernel()}</li>
 * </ul>
 * 热启动模板应用/更新不触发重启，Push 后立即生效。
 */
@Service
public class ConfigTemplateServiceImpl implements ConfigTemplateService {

    private static final Logger log = LoggerFactory.getLogger(ConfigTemplateServiceImpl.class);

    private static final String ACTION_KERNEL_RESTART = "KERNEL_RESTART";
    private static final String RESOURCE_KERNEL = "kernel";

    /** 冷启动模板 ID — 修改后需重启引擎生效，应用时强制重启 */
    private static final String COLD_TEMPLATE_ID = "tpl_instance_cold";
    /** 热启动模板 ID — 修改后立即生效，无需重启 */
    private static final String HOT_TEMPLATE_ID = "tpl_instance_hot";

    private final ConfigTemplateMapper templateMapper;
    private final TenantScopeConfigMapper tenantScopeConfigMapper;
    private final InstanceConfigMapper instanceConfigMapper;
    private final TenantMapper tenantMapper;
    private final ConfigAuditLogMapper auditLogMapper;
    private final ObjectMapper objectMapper;
    private final MemoryEngineClient memoryEngineClient;
    private final ScopeRegistryService scopeRegistryService;
    private final PermissionChecker permissionChecker;
    private final ConfirmTokenService confirmTokenService;

    public ConfigTemplateServiceImpl(ConfigTemplateMapper templateMapper,
                                     TenantScopeConfigMapper tenantScopeConfigMapper,
                                     InstanceConfigMapper instanceConfigMapper,
                                     TenantMapper tenantMapper,
                                     ConfigAuditLogMapper auditLogMapper,
                                     ObjectMapper objectMapper,
                                     MemoryEngineClient memoryEngineClient,
                                     ScopeRegistryService scopeRegistryService,
                                     PermissionChecker permissionChecker,
                                     ConfirmTokenService confirmTokenService) {
        this.templateMapper = templateMapper;
        this.tenantScopeConfigMapper = tenantScopeConfigMapper;
        this.instanceConfigMapper = instanceConfigMapper;
        this.tenantMapper = tenantMapper;
        this.auditLogMapper = auditLogMapper;
        this.objectMapper = objectMapper;
        this.memoryEngineClient = memoryEngineClient;
        this.scopeRegistryService = scopeRegistryService;
        this.permissionChecker = permissionChecker;
        this.confirmTokenService = confirmTokenService;
    }

    @Override
    public List<ConfigTemplateListItemDTO> list(String type, Boolean isBuiltin) {
        List<ConfigTemplateEntity> templates = templateMapper.selectList(
            new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<ConfigTemplateEntity>()
                .eq(type != null, "template_type", type)
                .eq(isBuiltin != null, "is_builtin", isBuiltin)
                .orderByDesc("is_builtin")
                .orderByAsc("template_name")
        );
        if (templates.isEmpty()) {
            return Collections.emptyList();
        }

        List<TenantScopeConfigEntity> usageEntities = tenantScopeConfigMapper.selectList(
            new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<TenantScopeConfigEntity>()
                .isNotNull("template_id"));
        Map<String, List<TemplateTenantUsageDTO>> usageByTemplate = usageEntities.stream()
            .collect(Collectors.groupingBy(
                TenantScopeConfigEntity::getTemplateId,
                java.util.LinkedHashMap::new,
                Collectors.mapping(
                    e -> TemplateTenantUsageDTO.builder()
                        .tenantId(e.getTenantId())
                        .tenantName(e.getTenantName())
                        .build(),
                    Collectors.toList())));

        return templates.stream().map(t -> ConfigTemplateListItemDTO.builder()
            .id(t.getId())
            .templateName(t.getTemplateName())
            .displayName(t.getDisplayName())
            .description(t.getDescription())
            .templateType(t.getTemplateType())
            .isBuiltin(t.getIsBuiltin())
            .parentId(t.getParentId())
            .version(t.getVersion())
            .status(t.getStatus())
            .createdBy(t.getCreatedBy())
            .createdAt(t.getCreatedAt())
            .updatedAt(t.getUpdatedAt())
            .tenantUsage(usageByTemplate.getOrDefault(t.getId(), Collections.emptyList()))
            .build()).toList();
    }

    @Override
    public ConfigTemplateEntity get(String id) {
        ConfigTemplateEntity t = templateMapper.selectById(id);
        if (t == null) {
            throw new BizException(ResultCode.NOT_FOUND, "模板不存在: " + id);
        }
        return t;
    }

    @Override
    @Transactional
    public TemplateApplyResultDTO create(CreateTemplateRequest request, String operator) {
        if (request.getTemplateType() == null ||
            (!"SCOPE".equals(request.getTemplateType()) && !"INSTANCE".equals(request.getTemplateType()))) {
            throw new BizException(ResultCode.BAD_REQUEST, "template_type 必须为 SCOPE 或 INSTANCE");
        }
        if (request.getTemplateName() == null || request.getTemplateName().isBlank()) {
            throw new BizException(ResultCode.BAD_REQUEST, "template_name 必填");
        }
        if (request.getConfigJson() == null || request.getConfigJson().isBlank()) {
            throw new BizException(ResultCode.BAD_REQUEST, "config_json 必填");
        }

        ConfigTemplateEntity template = new ConfigTemplateEntity();
        template.setId(UUID.randomUUID().toString());
        template.setTemplateName(request.getTemplateName());
        template.setDisplayName(request.getDisplayName() != null ? request.getDisplayName() : request.getTemplateName());
        template.setDescription(request.getDescription());
        template.setTemplateType(request.getTemplateType());
        template.setConfigJson(request.getConfigJson());
        template.setIsBuiltin(0);
        template.setParentId(request.getParentId());
        template.setVersion(1);
        template.setCreatedBy(operator);
        Instant now = Instant.now();
        template.setCreatedAt(now);
        template.setUpdatedAt(now);
        templateMapper.insert(template);
        recordAudit(operator, template.getId(), "TEMPLATE_CREATE", null, template.getConfigJson(), true, null, request.getReason());

        log.info("模板创建成功: id={}, name={}, type={}", template.getId(), template.getTemplateName(), template.getTemplateType());

        // 如果创建时带 targetTenantIds 或 INSTANCE 类型自动应用
        if ("INSTANCE".equals(request.getTemplateType())) {
            // 创建场景不携带 confirm_token，仅 Push 不重启；
            // 冷启动模板需用户后续通过 apply 接口（带 confirm_token）应用以触发重启
            return applyToInstance(template, operator, request.getReason(), false, null);
        } else if (request.getTargetTenantIds() != null && !request.getTargetTenantIds().isEmpty()) {
            return applyToTenants(template, request.getTargetTenantIds(), operator, request.getReason());
        }
        return TemplateApplyResultDTO.builder().templateId(template.getId())
            .templateName(template.getTemplateName()).templateType(template.getTemplateType())
            .results(new ArrayList<>()).successCount(0).failCount(0).build();
    }

    @Override
    @Transactional
    public TemplateApplyResultDTO copy(String sourceId, CreateTemplateRequest request, String operator) {
        ConfigTemplateEntity source = get(sourceId);
        if (request.getTemplateName() == null || request.getTemplateName().isBlank()) {
            request.setTemplateName(source.getTemplateName() + "_copy");
        }
        if (request.getDisplayName() == null) {
            request.setDisplayName(source.getDisplayName() + " (副本)");
        }
        if (request.getTemplateType() == null) {
            request.setTemplateType(source.getTemplateType());
        }
        if (request.getConfigJson() == null) {
            request.setConfigJson(source.getConfigJson());
        }
        if (request.getDescription() == null) {
            request.setDescription("复制自: " + source.getDisplayName());
        }
        request.setParentId(sourceId);
        return create(request, operator);
    }

    @Override
    @Transactional
    public ConfigTemplateEntity update(String id, UpdateTemplateRequest request, String operator) {
        ConfigTemplateEntity t = get(id);
        // 预置模板可修改（放开限制）
        if (request.getDisplayName() != null) t.setDisplayName(request.getDisplayName());
        if (request.getDescription() != null) t.setDescription(request.getDescription());
        if (request.getConfigJson() != null && !request.getConfigJson().equals(t.getConfigJson())) {
            // INSTANCE 类型：自动同步到 instance_config 单例
            if ("INSTANCE".equals(t.getTemplateType())) {
                // 仅在 apply=true 时推送到内核（草稿模式不推送）
                if (request.isApply()) {
                    // 冷启动模板更新强制重启；热启动模板不重启（忽略前端 restart 标记）
                    boolean needRestart = isColdTemplate(t.getId());
                    String token = needRestart ? request.getConfirmToken() : null;
                    // 校验重启权限与令牌（仅冷启动模板需要）
                    validateInstanceRestart(needRestart, token, operator);

                    InstanceConfigEntity ic = instanceConfigMapper.selectById(1);
                    if (ic != null) {
                        ic.setConfigJson(request.getConfigJson());
                        ic.setTemplateId(t.getId());
                        ic.setVersion(ic.getVersion() + 1);
                        ic.setUpdatedAt(Instant.now());
                        ic.setUpdatedBy(operator);
                        instanceConfigMapper.updateById(ic);
                        recordAudit(operator, null, "INSTANCE_CONFIG_UPDATE", null, request.getConfigJson(),
                            true, null, request.getReason());
                    }
                    boolean pushSuccess = pushInstanceConfig(request.getConfigJson());
                    boolean restartTriggered = needRestart && pushSuccess && restartKernel();
                    recordInstanceRestartAudit(operator, request.getConfigJson(), pushSuccess, restartTriggered,
                        request.getReason());
                }
            }
            t.setConfigJson(request.getConfigJson());
            t.setVersion(t.getVersion() + 1);
        }
        t.setUpdatedAt(Instant.now());
        // 设置状态：apply=true 时为 published，否则为 draft
        t.setStatus(request.isApply() ? "published" : "draft");
        templateMapper.updateById(t);
        recordAudit(operator, id, "TEMPLATE_UPDATE", null, t.getConfigJson(), true, null, request.getReason());

        // apply=true 时，自动应用到已绑定的租户 + 本次编辑新选的目标租户（仅 SCOPE 类型）。
        // 修复：原实现只对"已绑定"租户重新下发，导致编辑页选的"应用目标租户"被丢弃，
        // 新选的租户不会被绑定，列表 tenant_usage 为空 → 无"管理租户"按钮，再编辑进去也无值。
        if (request.isApply() && "SCOPE".equals(t.getTemplateType())) {
            List<String> boundTenantIds = tenantScopeConfigMapper.selectList(
                new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<TenantScopeConfigEntity>()
                    .eq("template_id", id)
                    .isNotNull("template_id")
            ).stream().map(TenantScopeConfigEntity::getTenantId).collect(Collectors.toList());
            // 合并：已绑定租户 + 本次新选目标租户（去重，保持顺序）
            List<String> targetTenantIds = request.getTargetTenantIds();
            List<String> mergedTenantIds = new ArrayList<>(boundTenantIds);
            if (targetTenantIds != null) {
                for (String tid : targetTenantIds) {
                    if (tid != null && !tid.isBlank() && !mergedTenantIds.contains(tid)) {
                        mergedTenantIds.add(tid);
                    }
                }
            }
            // 校验收敛在公共函数 applyToTenants() 入口（租户列表非空），此处不再重复判断。
            applyToTenants(t, mergedTenantIds, operator, request.getReason());
        }

        return t;
    }

    @Override
    @Transactional
    public TemplateDeleteResultDTO delete(String id, String operator) {
        ConfigTemplateEntity t = get(id);
        if (t.getIsBuiltin() != null && t.getIsBuiltin() == 1) {
            throw new BizException(ResultCode.FORBIDDEN, "预置模板不可删除");
        }

        // 查询所有绑定该模板的租户配置记录
        List<TenantScopeConfigEntity> boundConfigs = tenantScopeConfigMapper.selectList(
            new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<TenantScopeConfigEntity>()
                .eq("template_id", id));

        List<TemplateDeleteResultDTO.ScopeCleanupResult> cleanedScopes = new ArrayList<>();
        int kernelSuccess = 0, kernelFail = 0;

        // 级联清理：对每个绑定的租户，删除内核 scope 配置 + DB 绑定记录
        for (TenantScopeConfigEntity cfg : boundConfigs) {
            String tenantId = cfg.getTenantId();
            String tenantName = cfg.getTenantName();
            String scopeId = null;
            boolean kernelDeleted = false;
            boolean dbDeleted = false;
            String errorMsg = null;

            // 解析 scope_id（仅 SCOPE 类型模板绑定的租户才有 scope 配置需清理）
            try {
                scopeId = resolveScopeId(tenantId);
            } catch (Exception e) {
                errorMsg = "解析 scope_id 失败: " + e.getMessage();
                log.warn("删除模板时解析 scope_id 失败: templateId={}, tenantId={}, error={}",
                    id, tenantId, e.getMessage());
            }

            // 删除内核 scope 配置
            if (scopeId != null) {
                try {
                    kernelDeleted = memoryEngineClient.deleteScopeConfig(scopeId);
                    if (kernelDeleted) {
                        kernelSuccess++;
                    } else {
                        kernelFail++;
                        errorMsg = "内核返回删除失败";
                    }
                } catch (Exception e) {
                    kernelFail++;
                    errorMsg = "内核 scope 配置删除异常: " + e.getMessage();
                    log.warn("删除模板时内核 scope 配置删除失败: templateId={}, tenantId={}, scopeId={}, error={}",
                        id, tenantId, scopeId, e.getMessage(), e);
                }
            }

            // 删除 DB 绑定记录
            try {
                tenantScopeConfigMapper.deleteById(cfg.getTenantId());
                dbDeleted = true;
            } catch (Exception e) {
                log.warn("删除模板时 DB 绑定记录删除失败: templateId={}, tenantId={}, error={}",
                    id, tenantId, e.getMessage(), e);
                if (errorMsg == null) errorMsg = "DB 绑定记录删除失败: " + e.getMessage();
            }

            cleanedScopes.add(TemplateDeleteResultDTO.ScopeCleanupResult.builder()
                .tenantId(tenantId)
                .tenantName(tenantName)
                .scopeId(scopeId)
                .kernelDeleted(kernelDeleted)
                .dbBindingDeleted(dbDeleted)
                .errorMessage(errorMsg)
                .build());
        }

        // 删除模板本身
        templateMapper.deleteById(id);
        recordAudit(operator, id, "TEMPLATE_DELETE", null, null, true, null, null);

        log.info("模板删除成功: id={}, name={}, 绑定租户数={}, 内核清理成功={}, 内核清理失败={}",
            id, t.getTemplateName(), boundConfigs.size(), kernelSuccess, kernelFail);

        return TemplateDeleteResultDTO.builder()
            .templateId(id)
            .templateName(t.getTemplateName())
            .cleanedScopes(cleanedScopes)
            .kernelSuccessCount(kernelSuccess)
            .kernelFailCount(kernelFail)
            .build();
    }

    @Override
    @Transactional
    public TemplateApplyResultDTO apply(ApplyTemplateRequest request, String operator) {
        ConfigTemplateEntity t = get(request.getTemplateId());
        if ("INSTANCE".equals(t.getTemplateType())) {
            // 冷启动模板应用强制重启；热启动模板不重启（忽略前端 restart 标记）
            boolean needRestart = isColdTemplate(t.getId());
            String token = needRestart ? request.getConfirmToken() : null;
            return applyToInstance(t, operator, request.getReason(), needRestart, token);
        } else {
            // 校验收敛在公共函数 applyToTenants() 入口（租户列表非空），此处不再重复判断。
            return applyToTenants(t, request.getTargetTenantIds(), operator, request.getReason());
        }
    }

    private TemplateApplyResultDTO applyToInstance(ConfigTemplateEntity t, String operator, String reason,
                                                    boolean restart, String confirmToken) {
        // 校验重启权限与令牌
        validateInstanceRestart(restart, confirmToken, operator);

        InstanceConfigEntity ic = instanceConfigMapper.selectById(1);
        if (ic == null) {
            ic = new InstanceConfigEntity();
            ic.setId(1);
            ic.setVersion(1);
        } else {
            ic.setVersion(ic.getVersion() + 1);
        }
        ic.setTemplateId(t.getId());
        ic.setConfigJson(t.getConfigJson());
        ic.setUpdatedAt(Instant.now());
        ic.setUpdatedBy(operator);
        instanceConfigMapper.insertOrUpdate(ic);

        boolean pushSuccess = pushInstanceConfig(t.getConfigJson());
        boolean restartTriggered = restart && pushSuccess && restartKernel();
        String restartStatus = restartTriggered ? "in_progress" : (restart ? "failed" : "skipped");

        recordAudit(operator, null, "INSTANCE_CONFIG_UPDATE", null, t.getConfigJson(),
            true, null, reason);
        recordInstanceRestartAudit(operator, t.getConfigJson(), pushSuccess, restartTriggered, reason);

        return TemplateApplyResultDTO.builder()
            .templateId(t.getId()).templateName(t.getTemplateName()).templateType("INSTANCE")
            .successCount(1).failCount(0).results(new ArrayList<>())
            .restartTriggered(restartTriggered)
            .restartStatus(restartStatus)
            .build();
    }

    private TemplateApplyResultDTO applyToTenants(ConfigTemplateEntity t, List<String> tenantIds, String operator, String reason) {
        // 公共出口校验（唯一真源）：SCOPE 配置下发到内核必须有目标租户，
        // scope_id 来自租户绑定的 scope（tenants.scope_ids），无目标租户 → 无 scope_id → 无法下发。
        // update()/apply()/create() 均调用本函数，校验收敛于此，入口层不再重复判断。
        if (tenantIds == null || tenantIds.isEmpty()) {
            throw new BizException(ResultCode.BAD_REQUEST,
                "SCOPE 模板下发到内核必须指定至少一个目标租户：当前未选择目标租户且无已绑定租户，" +
                "无法确定 scope_id，配置无法下发到内核。请在编辑页选择《应用目标租户》后再保存发布。");
        }
        List<TemplateApplyResultDTO.TenantApplyResult> results = new ArrayList<>();
        int success = 0, fail = 0;
        for (String tenantId : tenantIds) {
            try {
                Tenant tenant = tenantMapper.selectById(tenantId);
                if (tenant == null) {
                    results.add(TemplateApplyResultDTO.TenantApplyResult.builder()
                        .tenantId(tenantId).success(false)
                        .errorMessage("租户不存在").build());
                    fail++;
                    continue;
                }
                TenantScopeConfigEntity existing = tenantScopeConfigMapper.findByTenantId(tenantId);
                if (existing == null) {
                    existing = new TenantScopeConfigEntity();
                    existing.setTenantId(tenantId);
                    existing.setTenantName(tenant.getName());
                    existing.setInstanceId("default");
                }
                existing.setConfigJson(t.getConfigJson());
                existing.setTemplateId(t.getId());
                existing.setTemplateVersion(t.getVersion());
                existing.setCurrentVersion(t.getVersion()); // 重置为模板版本
                existing.setUpdatedAt(Instant.now());
                existing.setUpdatedBy(operator);
                if (existing.getInstanceId() == null) existing.setInstanceId("default");
                tenantScopeConfigMapper.insertOrUpdate(existing);
                memoryEngineClient.setScopeConfig(resolveScopeId(tenantId), parseScopeConfigJson(t.getConfigJson()));
                recordAudit(operator, tenantId, "TEMPLATE_APPLY_TENANT", t.getConfigJson(),
                    t.getConfigJson(), true, null, reason);
                results.add(TemplateApplyResultDTO.TenantApplyResult.builder()
                    .tenantId(tenantId).tenantName(tenant.getName()).success(true)
                    .currentVersion(t.getVersion()).build());
                success++;
            } catch (Exception e) {
                // 记录服务端日志（原实现静默吞掉异常，导致问题无法排查）
                log.warn("SCOPE 模板下发到内核失败: templateId={}, tenantId={}, scopeId={}, error={}",
                    t.getId(), tenantId, resolveScopeIdSafe(tenantId), e.getMessage(), e);
                results.add(TemplateApplyResultDTO.TenantApplyResult.builder()
                    .tenantId(tenantId).success(false).errorMessage(e.getMessage()).build());
                fail++;
            }
        }
        // 全部租户下发失败时，抛出业务异常返回非 200 错误码给前端，
        // 避免前端误以为操作成功（原实现返回 HTTP 200 + failCount>0，前端只显示"模板已保存"）。
        if (success == 0 && fail > 0) {
            String failedTenants = results.stream()
                .map(r -> r.getTenantId() + ": " + r.getErrorMessage())
                .collect(Collectors.joining("; "));
            throw new BizException(ResultCode.UPSTREAM_ERROR,
                "配置下发到内核全部失败（" + fail + " 个租户）。失败详情: " + failedTenants +
                "。模板已保存但未生效，请检查内核服务状态后重试。");
        }
        return TemplateApplyResultDTO.builder()
            .templateId(t.getId()).templateName(t.getTemplateName()).templateType("SCOPE")
            .results(results).successCount(success).failCount(fail).build();
    }


    private void recordAudit(String operator, String resourceId, String operation,
                              String before, String after, boolean success,
                              String errorMsg, String reason) {
        ConfigAuditLogEntity audit = new ConfigAuditLogEntity();
        audit.setId(UUID.randomUUID().toString());
        audit.setOperatorId(operator);
        audit.setTemplateId(resourceId);
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
            throw new BizException(ResultCode.BAD_REQUEST, "租户未绑定 scope_id，无法应用 Scope 模板");
        }
        if (scopeIds.size() > 1) {
            throw new BizException(ResultCode.BAD_REQUEST, "租户绑定了多个 scope_id，当前配置中心仅支持一对一映射");
        }
        return scopeIds.get(0);
    }

    /**
     * resolveScopeId 的安全版本：不抛异常，用于 catch 块日志记录场景，
     * 避免在异常处理中再次抛出掩盖原始异常。
     */
    private String resolveScopeIdSafe(String tenantId) {
        try {
            return resolveScopeId(tenantId);
        } catch (Exception e) {
            return "(unresolved: " + e.getMessage() + ")";
        }
    }

    /** 解析 scope_ids JSON 数组 */
    private static List<String> parseScopeIds(String scopeIdsJson) {
        if (scopeIdsJson == null || scopeIdsJson.isBlank()) {
            return List.of();
        }
        try {
            if (scopeIdsJson.trim().startsWith("[")) {
                ObjectMapper mapper = new ObjectMapper();
                return mapper.readValue(scopeIdsJson, new TypeReference<List<String>>() {});
            }
            return List.of(scopeIdsJson.split(","));
        } catch (Exception e) {
            return List.of();
        }
    }

    /**
     * INSTANCE 模板重启权限 + 令牌校验。
     * <p>
     * 与 KernelConfigController 保持一致：restart=true 时要求 kernel:restart 权限，
     * 并校验 confirm_token（ACTION_KERNEL_RESTART / RESOURCE_KERNEL）。
     */
    private void validateInstanceRestart(boolean restart, String confirmToken, String operator) {
        if (!restart) {
            return;
        }
        permissionChecker.require("kernel:restart");
        if (confirmToken == null || confirmToken.isBlank()) {
            throw new BizException(ResultCode.CONFIRM_TOKEN_INVALID,
                "重启操作需要二次确认令牌（confirm_token），请先调用 GET /api/v1/config/kernel/confirm-token 获取");
        }
        if (!confirmTokenService.validate(confirmToken, operator, ACTION_KERNEL_RESTART, RESOURCE_KERNEL)) {
            throw new BizException(ResultCode.CONFIRM_TOKEN_INVALID, "确认令牌无效或已过期");
        }
        confirmTokenService.consume(confirmToken);
    }

    /**
     * 判断是否为冷启动模板（修改后需重启引擎生效）。
     * 冷启动模板 ID 为 tpl_instance_cold；其余 INSTANCE 模板视为热启动。
     */
    private boolean isColdTemplate(String templateId) {
        return COLD_TEMPLATE_ID.equals(templateId);
    }

    private boolean pushInstanceConfig(String configJson) {
        try {
            memoryEngineClient.pushKernelConfig(parseInstanceConfigJson(configJson));
            return true;
        } catch (Exception e) {
            log.warn("INSTANCE 模板 Push 到内核失败: {}", e.getMessage());
            return false;
        }
    }

    private boolean restartKernel() {
        try {
            memoryEngineClient.restartKernel();
            return true;
        } catch (Exception e) {
            log.warn("INSTANCE 模板触发内核重启失败: {}", e.getMessage());
            return false;
        }
    }

    private void recordInstanceRestartAudit(String operator, String configJson,
                                             boolean pushSuccess, boolean restartTriggered,
                                             String reason) {
        if (restartTriggered) {
            recordAudit(operator, null, "INSTANCE_CONFIG_RESTART", null,
                toJsonString(Map.of(
                    "config_json", configJson != null ? configJson : "",
                    "push_success", pushSuccess,
                    "restart_triggered", restartTriggered
                )), true, null, reason);
        }
    }

    @SuppressWarnings("unchecked")
    private String toJsonString(Object obj) {
        try {
            return objectMapper.writeValueAsString(obj);
        } catch (Exception e) {
            return "{}";
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> parseScopeConfigJson(String configJson) {
        try {
            Map<String, Object> config = objectMapper.readValue(configJson, Map.class);
            // 自动填充空的 API_KEY 从环境变量
            fillEmptyApiKeysFromEnv(config);
            return config;
        } catch (Exception e) {
            throw new BizException(ResultCode.BAD_REQUEST, "模板配置不是合法的 Scope JSON: " + e.getMessage());
        }
    }

    /**
     * 当模板配置中的 API_KEY 为空时，从环境变量或内核 .env 文件自动填充
     */
    @SuppressWarnings("unchecked")
    private void fillEmptyApiKeysFromEnv(Map<String, Object> config) {
        // 不再自动填充 API Key，由用户手动输入
    }

    @SuppressWarnings("unchecked")
    private Map<String, String> parseInstanceConfigJson(String configJson) {
        try {
            Map<String, Object> raw = objectMapper.readValue(configJson, Map.class);
            Map<String, String> updates = new LinkedHashMap<>();
            for (Map.Entry<String, Object> entry : raw.entrySet()) {
                updates.put(entry.getKey(), entry.getValue() == null ? "" : String.valueOf(entry.getValue()));
            }
            return updates;
        } catch (Exception e) {
            throw new BizException(ResultCode.BAD_REQUEST, "模板配置不是合法的 INSTANCE JSON: " + e.getMessage());
        }
    }
}
