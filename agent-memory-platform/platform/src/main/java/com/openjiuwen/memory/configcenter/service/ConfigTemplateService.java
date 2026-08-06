package com.openjiuwen.memory.configcenter.service;

import com.openjiuwen.memory.configcenter.domain.ConfigTemplateEntity;
import com.openjiuwen.memory.configcenter.dto.ApplyTemplateRequest;
import com.openjiuwen.memory.configcenter.dto.ConfigTemplateListItemDTO;
import com.openjiuwen.memory.configcenter.dto.CreateTemplateRequest;
import com.openjiuwen.memory.configcenter.dto.TemplateApplyResultDTO;
import com.openjiuwen.memory.configcenter.dto.TemplateDeleteResultDTO;
import com.openjiuwen.memory.configcenter.dto.UpdateTemplateRequest;

import java.util.List;

/**
 * 模板管理服务 — 2026-07-17 P0-3 v2 重构
 * <p>
 * 简化为 2 种类型 SCOPE / INSTANCE，支持创建/复制/应用/修改/删除
 */
public interface ConfigTemplateService {

    /** 列出所有模板（按 type + isBuiltin 过滤），附带当前使用租户 */
    List<ConfigTemplateListItemDTO> list(String type, Boolean isBuiltin);

    /** 查模板详情 */
    ConfigTemplateEntity get(String id);

    /** 创建模板（带可选 targetTenantIds 时同事务内应用） */
    TemplateApplyResultDTO create(CreateTemplateRequest request, String operator);

    /** 复制模板（从 sourceId 复制，parentId=sourceId） */
    TemplateApplyResultDTO copy(String sourceId, CreateTemplateRequest request, String operator);

    /** 修改模板参数（预置不可改） */
    ConfigTemplateEntity update(String id, UpdateTemplateRequest request, String operator);

    /**
     * 删除模板（预置不可删）。
     * <p>
     * 若模板被租户绑定，会级联清理：
     * <ul>
     *   <li>内核：调用 delete_scope_config 删除每个绑定租户的 scope 配置</li>
     *   <li>DB：删除 tenant_scope_configs 绑定记录</li>
     * </ul>
     * 返回受影响的 scope 清理详情，供前端展示。
     */
    TemplateDeleteResultDTO delete(String id, String operator);

    /** 应用模板到租户（SCOPE 必填 targetTenantIds，INSTANCE 自动全局） */
    TemplateApplyResultDTO apply(ApplyTemplateRequest request, String operator);
}
