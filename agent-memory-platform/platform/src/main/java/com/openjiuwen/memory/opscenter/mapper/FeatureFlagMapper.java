package com.openjiuwen.memory.opscenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.opscenter.domain.FeatureFlagEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Select;

@Mapper
public interface FeatureFlagMapper extends BaseMapper<FeatureFlagEntity> {

    @Select("SELECT * FROM feature_flag WHERE tenant_id = #{tenantId} AND scope_id = #{scopeId} LIMIT 1")
    FeatureFlagEntity findByTenantIdAndScopeId(String tenantId, String scopeId);
}
