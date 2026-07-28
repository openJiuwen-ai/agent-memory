package com.openjiuwen.memory.authcenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 用户实体类
 */
@Data
@TableName("users")
public class User {
    
    /**
     * 用户ID（UUID）
     */
    @TableId(type = IdType.ASSIGN_UUID)
    private String id;
    
    /**
     * 所属租户ID
     */
    @com.baomidou.mybatisplus.annotation.TableField("tenant_id")
    private String tenantId;
    
    /**
     * 用户名
     */
    private String username;
    
    /**
     * 密码（BCrypt加密）
     */
    private String password;
    
    /**
     * 角色：SUPER_ADMIN/PLATFORM_ADMIN/SECURITY_ADMIN/SCOPE_ADMIN/READ_ONLY
     */
    private String role;
    
    /**
     * Scope权限列表（JSON数组）
     */
    @com.baomidou.mybatisplus.annotation.TableField("scope_ids")
    private String scopeIds;
    
    /**
     * 创建时间
     */
    private LocalDateTime createdAt;
    
    /**
     * 更新时间
     */
    private LocalDateTime updatedAt;
    
    /**
     * 备注
     */
    private String remark;
}
