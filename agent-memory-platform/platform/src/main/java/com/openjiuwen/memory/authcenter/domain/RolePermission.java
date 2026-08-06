package com.openjiuwen.memory.authcenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

/**
 * 角色权限实体类
 */
@Data
@TableName("role_permissions")
public class RolePermission {
    
    /**
     * 主键ID
     */
    @TableId(type = IdType.ASSIGN_UUID)
    private String id;
    
    /**
     * 角色名称
     */
    private String role;
    
    /**
     * 权限点
     */
    private String permission;
}
