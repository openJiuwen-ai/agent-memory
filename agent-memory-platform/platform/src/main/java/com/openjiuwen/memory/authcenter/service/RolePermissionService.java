package com.openjiuwen.memory.authcenter.service;

import com.baomidou.mybatisplus.extension.service.IService;
import com.openjiuwen.memory.authcenter.domain.RolePermission;

import java.util.List;

/**
 * 角色权限服务接口
 */
public interface RolePermissionService extends IService<RolePermission> {
    
    /**
     * 根据角色查询权限列表
     * @param role 角色名称
     * @return 权限列表
     */
    List<String> getPermissionsByRole(String role);
}
