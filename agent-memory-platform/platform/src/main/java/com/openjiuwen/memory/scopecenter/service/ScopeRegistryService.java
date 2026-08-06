package com.openjiuwen.memory.scopecenter.service;

import com.baomidou.mybatisplus.extension.service.IService;
import com.openjiuwen.memory.scopecenter.domain.ScopeRegistry;
import com.openjiuwen.memory.scopecenter.dto.ScopeStatsDTO;

import java.util.List;

/**
 * Scope注册表服务接口
 */
public interface ScopeRegistryService extends IService<ScopeRegistry> {
    
    /**
     * 获取所有Scope列表
     */
    List<ScopeRegistry> getAllScopes();
    
    /**
     * 获取可分配的Scope列表（unassigned状态）
     */
    List<ScopeRegistry> getAvailableScopes();
    
    /**
     * 获取已分配的Scope列表
     */
    List<ScopeRegistry> getAssignedScopes();
    
    /**
     * 根据租户ID获取已分配的Scope列表
     */
    List<ScopeRegistry> getScopesByTenantId(String tenantId);
    
    /**
     * 分配Scope给租户
     */
    void assignScopeToTenant(String scopeId, String tenantId);
    
    /**
     * 释放Scope（取消分配）
     */
    void releaseScope(String scopeId);
    
    /**
     * 批量分配Scope给租户
     */
    void batchAssignScopesToTenant(List<String> scopeIds, String tenantId);
    
    /**
     * 批量释放 Scope
     */
    void batchReleaseScopes(List<String> scopeIds);
    
    /**
     * 获取指定 Scope 的统计信息（包含绑定租户状态）
     */
    ScopeStatsDTO getScopeStats(String scopeId);
    
    /**
     * 检查 scope_id 是否已存在
     */
    boolean existsByScopeId(String scopeId);
    
    /**
     * 根据 scope_id 查询 Scope
     */
    ScopeRegistry getByScopeId(String scopeId);
}
