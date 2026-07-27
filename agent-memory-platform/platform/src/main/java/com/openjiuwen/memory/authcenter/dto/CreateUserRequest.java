package com.openjiuwen.memory.authcenter.dto;

import lombok.Data;

import java.util.List;

/**
 * 创建用户请求DTO
 */
@Data
public class CreateUserRequest {
    
    /**
     * 用户名
     */
    private String username;
    
    /**
     * 密码
     */
    private String password;
    
    /**
     * 角色：PLATFORM_ADMIN/SECURITY_ADMIN/SCOPE_ADMIN/READ_ONLY/VIEWER
     */
    private String role;
    
    /**
     * 所属租户ID
     */
    private String tenantId;
    
    /**
     * Scope权限列表
     */
    private List<String> scopeIds;
    
    /**
     * 备注
     */
    private String remark;
}
