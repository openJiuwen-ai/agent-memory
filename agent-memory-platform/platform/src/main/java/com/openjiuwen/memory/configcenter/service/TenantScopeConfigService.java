package com.openjiuwen.memory.configcenter.service;

import com.openjiuwen.memory.configcenter.domain.TenantScopeConfigEntity;
import com.openjiuwen.memory.configcenter.dto.TenantScopeConfigDTO;
import com.openjiuwen.memory.configcenter.dto.TenantScopeConfigDeleteResultDTO;
import com.openjiuwen.memory.configcenter.dto.TenantScopeConfigListItemDTO;

import java.util.List;

/**
 * 租户级 Scope 配置服务 — 2026-07-17 P0-3 v2 重构
 * <p>
 * 1 tenant = 1 scope，UUID 同体，PK = tenant_id
 */
public interface TenantScopeConfigService {

    /** 获取租户的 Scope 配置快照 */
    TenantScopeConfigDTO getByTenant(String tenantId);

    /** 租户修改自己的参数（不影响其他租户） */
    TenantScopeConfigDTO update(String tenantId, String configJson, String operator);

    /**
     * 清除租户的 Scope 配置：删除内核 KV 中的 scope 配置 + DB 绑定记录，
     * 使该租户回退到默认配置。租户本身不删除。
     */
    TenantScopeConfigDeleteResultDTO delete(String tenantId, String operator);

    /** 列出所有租户的快照（平台管理员视图，列表项不含 config_json） */
    List<TenantScopeConfigListItemDTO> listAll();

    /** 按模板 ID 过滤列出租户快照（列表项不含 config_json，templateId 必填） */
    List<TenantScopeConfigListItemDTO> listAll(String templateId);

    /** 列出偏离模板的租户（列表项不含 config_json，templateVersion != currentVersion） */
    List<TenantScopeConfigListItemDTO> listDeviated();

    /** 平台操作：把租户快照同步回模板（即"以原模板下发"） */
    TenantScopeConfigDTO syncFromTemplate(String tenantId, String operator);
}
