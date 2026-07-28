package com.openjiuwen.memory.opscenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.opscenter.domain.TenantQuotaEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Select;

@Mapper
public interface TenantQuotaMapper extends BaseMapper<TenantQuotaEntity> {

    @Select("SELECT * FROM tenant_quotas WHERE admin_user_id = #{adminUserId} LIMIT 1")
    TenantQuotaEntity findByAdminUserId(String adminUserId);
}
