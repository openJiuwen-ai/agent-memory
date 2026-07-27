package com.openjiuwen.memory.scopecenter.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.openjiuwen.memory.scopecenter.domain.ScopeRegistry;
import com.openjiuwen.memory.scopecenter.mapper.ScopeRegistryMapper;
import com.openjiuwen.memory.scopecenter.service.ScopeRegistryService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

/**
 * Scope注册表服务实现类
 */
@Service
public class ScopeRegistryServiceImpl extends ServiceImpl<ScopeRegistryMapper, ScopeRegistry> implements ScopeRegistryService {
    
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
}
