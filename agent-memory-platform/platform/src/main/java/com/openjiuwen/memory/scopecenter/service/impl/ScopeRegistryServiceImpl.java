package com.openjiuwen.memory.scopecenter.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.openjiuwen.memory.configcenter.domain.TenantScopeConfigEntity;
import com.openjiuwen.memory.configcenter.mapper.TenantScopeConfigMapper;
import com.openjiuwen.memory.scopecenter.domain.ScopeRegistry;
import com.openjiuwen.memory.scopecenter.dto.ScopeStatsDTO;
import com.openjiuwen.memory.scopecenter.mapper.ScopeRegistryMapper;
import com.openjiuwen.memory.scopecenter.service.ScopeRegistryService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

/**
 * Scope注册表服务实现类
 */
@Service
public class ScopeRegistryServiceImpl extends ServiceImpl<ScopeRegistryMapper, ScopeRegistry> implements ScopeRegistryService {
    
    @Autowired
    private TenantScopeConfigMapper tenantScopeConfigMapper;
    
    @Override
    public List<ScopeRegistry> getAllScopes() {
        return list();
    }
    
    @Override
    public List<ScopeRegistry> getAvailableScopes() {
        LambdaQueryWrapper<ScopeRegistry> wrapper = new LambdaQueryWrapper<>();
        wrapper.eq(ScopeRegistry::getStatus, "unassigned");
        wrapper.orderByAsc(ScopeRegistry::getScopeId);
        return list(wrapper);
    }
    
    @Override
    public List<ScopeRegistry> getAssignedScopes() {
        LambdaQueryWrapper<ScopeRegistry> wrapper = new LambdaQueryWrapper<>();
        wrapper.eq(ScopeRegistry::getStatus, "assigned");
        wrapper.orderByAsc(ScopeRegistry::getScopeId);
        return list(wrapper);
    }
    
    @Override
    public List<ScopeRegistry> getScopesByTenantId(String tenantId) {
        LambdaQueryWrapper<ScopeRegistry> wrapper = new LambdaQueryWrapper<>();
        wrapper.eq(ScopeRegistry::getAssignedToTenantId, tenantId);
        wrapper.eq(ScopeRegistry::getStatus, "assigned");
        wrapper.orderByAsc(ScopeRegistry::getScopeId);
        return list(wrapper);
    }
    
    @Override
    @Transactional(rollbackFor = Exception.class)
    public void assignScopeToTenant(String scopeId, String tenantId) {
        LambdaUpdateWrapper<ScopeRegistry> wrapper = new LambdaUpdateWrapper<>();
        wrapper.eq(ScopeRegistry::getScopeId, scopeId)
               .eq(ScopeRegistry::getStatus, "unassigned")
               .set(ScopeRegistry::getStatus, "assigned")
               .set(ScopeRegistry::getAssignedToTenantId, tenantId)
               .set(ScopeRegistry::getUpdatedAt, LocalDateTime.now());
        
        boolean updated = update(wrapper);
        if (!updated) {
            throw new RuntimeException("Scope分配失败：Scope不存在或已被分配");
        }
    }
    
    @Override
    @Transactional(rollbackFor = Exception.class)
    public void releaseScope(String scopeId) {
        LambdaUpdateWrapper<ScopeRegistry> wrapper = new LambdaUpdateWrapper<>();
        wrapper.eq(ScopeRegistry::getScopeId, scopeId)
               .eq(ScopeRegistry::getStatus, "assigned")
               .set(ScopeRegistry::getStatus, "unassigned")
               .set(ScopeRegistry::getAssignedToTenantId, null)
               .set(ScopeRegistry::getUpdatedAt, LocalDateTime.now());
        
        update(wrapper);
    }
    
    @Override
    @Transactional(rollbackFor = Exception.class)
    public void batchAssignScopesToTenant(List<String> scopeIds, String tenantId) {
        for (String scopeId : scopeIds) {
            assignScopeToTenant(scopeId, tenantId);
        }
    }
    
    @Override
    @Transactional(rollbackFor = Exception.class)
    public void batchReleaseScopes(List<String> scopeIds) {
        for (String scopeId : scopeIds) {
            releaseScope(scopeId);
        }
    }
    
    @Override
    public ScopeStatsDTO getScopeStats(String scopeId) {
        // 1. 查询 Scope 基本信息
        ScopeRegistry scope = lambdaQuery()
                .eq(ScopeRegistry::getScopeId, scopeId)
                .one();
        
        if (scope == null) {
            return null;
        }
        
        // 2. 检查是否已绑定租户（通过 scope.status 和 assigned_to_tenant_id）
        boolean isBoundToTenant = "assigned".equals(scope.getStatus()) && 
                                  scope.getAssignedToTenantId() != null;
        
        // 3. 如果已绑定，从 tenant_scope_configs 表中获取租户名称
        String tenantName = null;
        String tenantId = null;
        
        if (isBoundToTenant) {
            TenantScopeConfigEntity tenantConfig = tenantScopeConfigMapper.selectById(
                    scope.getAssignedToTenantId());
            if (tenantConfig != null) {
                tenantId = tenantConfig.getTenantId();
                tenantName = tenantConfig.getTenantName();
            }
        }
        
        // 4. 构建返回的 DTO
        return ScopeStatsDTO.builder()
                .scopeId(scope.getScopeId())
                .scopeName(scope.getScopeName())
                .description(scope.getDescription())
                .boundToTenant(isBoundToTenant ? "yes" : "no")
                .tenantId(tenantId)
                .tenantName(tenantName)
                .build();
    }
    
    /**
     * 检查 scope_id 是否已存在
     */
    public boolean existsByScopeId(String scopeId) {
        LambdaQueryWrapper<ScopeRegistry> wrapper = new LambdaQueryWrapper<>();
        wrapper.eq(ScopeRegistry::getScopeId, scopeId);
        return count(wrapper) > 0;
    }
    
    /**
     * 根据 scope_id 查询 Scope
     */
    public ScopeRegistry getByScopeId(String scopeId) {
        LambdaQueryWrapper<ScopeRegistry> wrapper = new LambdaQueryWrapper<>();
        wrapper.eq(ScopeRegistry::getScopeId, scopeId);
        return getOne(wrapper);
    }
}
