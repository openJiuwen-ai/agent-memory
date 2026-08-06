package com.openjiuwen.memory.tenantcenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.tenantcenter.domain.Tenant;
import org.apache.ibatis.annotations.Mapper;

/**
 * 租户 Mapper 接口
 */
@Mapper
public interface TenantMapper extends BaseMapper<Tenant> {
}
