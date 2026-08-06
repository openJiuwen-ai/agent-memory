package com.openjiuwen.memory.tenantcenter.service.impl;

import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.openjiuwen.memory.tenantcenter.domain.Tenant;
import com.openjiuwen.memory.tenantcenter.mapper.TenantMapper;
import com.openjiuwen.memory.tenantcenter.service.TenantService;
import org.springframework.stereotype.Service;

/**
 * 租户服务实现类
 */
@Service
public class TenantServiceImpl extends ServiceImpl<TenantMapper, Tenant> implements TenantService {
}
