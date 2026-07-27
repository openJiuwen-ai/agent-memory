package com.openjiuwen.memory.authcenter.service;

import com.baomidou.mybatisplus.extension.service.IService;
import com.openjiuwen.memory.authcenter.domain.User;
import com.openjiuwen.memory.authcenter.dto.ChangePasswordRequest;
import com.openjiuwen.memory.authcenter.dto.CreateUserRequest;

/**
 * 用户服务接口
 */
public interface UserService extends IService<User> {
    
    /**
     * 根据用户名查询用户
     * @param username 用户名
     * @return 用户信息
     */
    User getByUsername(String username);
    
    /**
     * 创建用户（带权限校验）
     * 
     * @param currentUserRole 当前登录用户的角色
     * @param currentUserScopeIds 当前登录用户的scope权限
     * @param request 创建用户请求
     */
    User createUserWithPermissionCheck(String currentUserRole, String currentUserScopeIds, CreateUserRequest request);
    
    /**
     * 管理员重置用户密码（不需要原密码）
     */
    void resetPasswordByAdmin(String userId, String newPassword);
    
    /**
     * 用户修改自己的密码（需要验证原密码）
     */
    void changePassword(String userId, ChangePasswordRequest request);
    
    /**
     * 校验角色创建权限
     */
    boolean canCreateRole(String creatorRole, String targetRole);
    
    /**
     * 校验Scope分配权限
     */
    boolean canAssignScopes(String creatorRole, String creatorScopeIds, java.util.List<String> targetScopeIds);
}
