package com.openjiuwen.memory.authcenter.dto;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 用户信息视图对象（VO）
 * 用于对外返回用户信息，排除敏感字段（如 password）
 */
public class UserVO {
    
    private String id;
    private String username;
    private String role;
    private String scopeIds;
    private String tenantId;
    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;
    
    // Getters and Setters
    
    public String getId() {
        return id;
    }
    
    public void setId(String id) {
        this.id = id;
    }
    
    public String getUsername() {
        return username;
    }
    
    public void setUsername(String username) {
        this.username = username;
    }
    
    public String getRole() {
        return role;
    }
    
    public void setRole(String role) {
        this.role = role;
    }
    
    public String getScopeIds() {
        return scopeIds;
    }
    
    public void setScopeIds(String scopeIds) {
        this.scopeIds = scopeIds;
    }
    
    public String getTenantId() {
        return tenantId;
    }
    
    public void setTenantId(String tenantId) {
        this.tenantId = tenantId;
    }
    
    public LocalDateTime getCreatedAt() {
        return createdAt;
    }
    
    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }
    
    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }
    
    public void setUpdatedAt(LocalDateTime updatedAt) {
        this.updatedAt = updatedAt;
    }
    
    /**
     * 从 User 实体对象转换为 UserVO
     */
    public static UserVO fromUser(com.openjiuwen.memory.authcenter.domain.User user) {
        if (user == null) {
            return null;
        }
        UserVO vo = new UserVO();
        vo.setId(user.getId());
        vo.setUsername(user.getUsername());
        vo.setRole(user.getRole());
        vo.setScopeIds(user.getScopeIds());
        vo.setTenantId(user.getTenantId());
        vo.setCreatedAt(user.getCreatedAt());
        vo.setUpdatedAt(user.getUpdatedAt());
        // 注意：不复制 password 字段
        return vo;
    }
}
