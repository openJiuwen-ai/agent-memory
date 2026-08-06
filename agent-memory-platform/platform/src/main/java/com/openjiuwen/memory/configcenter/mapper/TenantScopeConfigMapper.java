package com.openjiuwen.memory.configcenter.mapper;

import com.baomidou.mybatisplus.annotation.InterceptorIgnore;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.configcenter.domain.TenantScopeConfigEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface TenantScopeConfigMapper extends BaseMapper<TenantScopeConfigEntity> {

    @Select("SELECT * FROM tenant_scope_configs WHERE tenant_id = #{tenantId}")
    TenantScopeConfigEntity findByTenantId(String tenantId);

    /** 列表视图专用列（不含 config_json 大字段） */
    String LIST_COLUMNS = "tenant_id, tenant_name, instance_id, template_id, template_version, current_version, updated_at, updated_by";

    /**
     * 列表视图专用：不取 config_json（列表展示用不到，避免大字段传输与内存浪费）。
     * 单条快照仍走 findByTenantId（需要完整 config_json 用于下发/比对）。
     */
    @Select("SELECT " + LIST_COLUMNS + " FROM tenant_scope_configs ORDER BY tenant_name")
    List<TenantScopeConfigEntity> listAll();

    /**
     * 按模板过滤的列表视图 + scope_ids：LEFT JOIN tenants 用 scope_ids（JSON 数组）获取绑定的 scope。
     * 绑定关系权威来源是 tenants.scope_ids（如 ["scope_02"]），JOIN 一次查出，避免逐行循环反查（N+1）。
     * SQLite 使用 json_each 解析 JSON，MySQL 使用 JSON_EXTRACT，GaussDB 使用 JSON_EXTRACT。
     */
    @InterceptorIgnore(tenantLine = "true")
    @Select("SELECT t.tenant_id, t.tenant_name, t.instance_id, t.template_id, t.template_version, " +
            "t.current_version, t.updated_at, t.updated_by, " +
            "tenant_scope.scope_ids AS scope_id " +
            "FROM tenant_scope_configs t " +
            "LEFT JOIN tenants tenant_scope ON tenant_scope.id = t.tenant_id " +
            "WHERE t.template_id = #{templateId} " +
            "ORDER BY t.tenant_name")
    List<TenantScopeConfigEntity> listByTemplateIdWithScope(String templateId);
}
