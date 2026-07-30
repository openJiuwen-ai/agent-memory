package com.openjiuwen.memory.authcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
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
     * Scope 权限列表
     */
    @JsonProperty("scopeIds")
    private List<String> scopeIds;
    
    /**
     * 备注
     */
    private String remark;
}
