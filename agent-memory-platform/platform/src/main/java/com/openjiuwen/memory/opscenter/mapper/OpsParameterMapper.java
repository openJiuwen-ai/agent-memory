package com.openjiuwen.memory.opscenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.opscenter.domain.OpsParameterEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Update;

@Mapper
public interface OpsParameterMapper extends BaseMapper<OpsParameterEntity> {

    /** SQLite 幂等 upsert：按 (tenant_id, scope_id, param_key) 唯一约束替换 */
    @Update("INSERT OR REPLACE INTO ops_parameter (tenant_id, scope_id, param_key, param_value, param_type, value_json, is_draft, updated_at, updated_by) "
            + "VALUES (#{tenantId}, #{scopeId}, #{paramKey}, #{paramValue}, #{paramType}, #{valueJson}, #{isDraft}, #{updatedAt}, #{updatedBy})")
    int upsertDraft(OpsParameterEntity entity);
}
