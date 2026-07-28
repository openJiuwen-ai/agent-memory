package com.openjiuwen.memory.authcenter.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.authcenter.domain.User;
import com.openjiuwen.memory.authcenter.dto.ChangePasswordRequest;
import com.openjiuwen.memory.authcenter.dto.CreateUserRequest;
import com.openjiuwen.memory.authcenter.mapper.UserMapper;
import com.openjiuwen.memory.authcenter.service.UserService;
import com.openjiuwen.memory.common.util.PasswordEncoder;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;

/**
 * 用户服务实现类
 */
@Service
public class UserServiceImpl extends ServiceImpl<UserMapper, User> implements UserService {
    
    private final ObjectMapper objectMapper = new ObjectMapper();
    
    @Override
    public User getByUsername(String username) {
        return baseMapper.selectOne(
            new LambdaQueryWrapper<User>().eq(User::getUsername, username)
        );
    }
    
    /**
     * 创建用户（带密码加密）
     */
    public User createUserWithPassword(User user) {
        user.setPassword(PasswordEncoder.encode(user.getPassword()));
        baseMapper.insert(user);
        return user;
    }
    
    /**
     * 更新用户密码
     */
    public boolean updatePassword(String userId, String newPassword) {
        User user = new User();
        user.setId(userId);
        user.setPassword(PasswordEncoder.encode(newPassword));
        return baseMapper.updateById(user) > 0;
    }
    
    @Override
    public User createUserWithPermissionCheck(String currentUserRole, String currentUserScopeIds, CreateUserRequest request) {
        // 1. 校验角色创建权限
        if (!canCreateRole(currentUserRole, request.getRole())) {
            throw new RuntimeException("无权限创建该角色：" + request.getRole());
        }
        
        // 2. 校验Scope分配权限
        if (request.getScopeIds() != null && !request.getScopeIds().isEmpty()) {
            if (!canAssignScopes(currentUserRole, currentUserScopeIds, request.getScopeIds())) {
                throw new RuntimeException("无权限分配这些Scope");
            }
        }
        
        // 3. 创建用户
        User user = new User();
        user.setId(UUID.randomUUID().toString().replace("-", ""));
        user.setUsername(request.getUsername());
        user.setPassword(PasswordEncoder.encode(request.getPassword()));
        user.setRole(request.getRole());
        user.setTenantId(request.getTenantId());  // 设置租户ID
        user.setRemark(request.getRemark());
        user.setCreatedAt(LocalDateTime.now());
        user.setUpdatedAt(LocalDateTime.now());
        
        // 设置scopeIds（JSON格式）
        if (request.getScopeIds() != null && !request.getScopeIds().isEmpty()) {
            try {
                user.setScopeIds(objectMapper.writeValueAsString(request.getScopeIds()));
            } catch (Exception e) {
                throw new RuntimeException("ScopeIds序列化失败", e);
            }
        }
        
        // 4. 保存用户
        boolean success = save(user);
        if (!success) {
            throw new RuntimeException("创建用户失败");
        }
        
        return user;
    }
    
    @Override
    public void resetPasswordByAdmin(String userId, String newPassword) {
        User user = getById(userId);
        if (user == null) {
            throw new RuntimeException("用户不存在");
        }
        
        // 超级管理员不能修改自己的密码（通过此接口）
        if ("SUPER_ADMIN".equals(user.getRole())) {
            throw new RuntimeException("不能修改超级管理员密码");
        }
        
        user.setPassword(PasswordEncoder.encode(newPassword));
        user.setUpdatedAt(LocalDateTime.now());
        
        updateById(user);
    }
    
    @Override
    public void changePassword(String userId, ChangePasswordRequest request) {
        User user = getById(userId);
        if (user == null) {
            throw new RuntimeException("用户不存在");
        }
        
        // 验证原密码
        if (!PasswordEncoder.matches(request.getOldPassword(), user.getPassword())) {
            throw new RuntimeException("原密码错误");
        }
        
        // 更新密码
        user.setPassword(PasswordEncoder.encode(request.getNewPassword()));
        user.setUpdatedAt(LocalDateTime.now());
        
        updateById(user);
    }
    
    @Override
    public boolean canCreateRole(String creatorRole, String targetRole) {
        // SUPER_ADMIN可以创建所有角色（除了SUPER_ADMIN本身）
        if ("SUPER_ADMIN".equals(creatorRole)) {
            return !"SUPER_ADMIN".equals(targetRole);
        }
        
        // SCOPE_ADMIN可以创建READ_ONLY和VIEWER
        if ("SCOPE_ADMIN".equals(creatorRole)) {
            return "READ_ONLY".equals(targetRole) || "VIEWER".equals(targetRole);
        }
        
        // PLATFORM_ADMIN和SECURITY_ADMIN不能创建其他用户
        return false;
    }
    
    @Override
    public boolean canAssignScopes(String creatorRole, String creatorScopeIds, List<String> targetScopeIds) {
        // SUPER_ADMIN、PLATFORM_ADMIN、SECURITY_ADMIN可以分配任意scope
        if ("SUPER_ADMIN".equals(creatorRole) || 
            "PLATFORM_ADMIN".equals(creatorRole) || 
            "SECURITY_ADMIN".equals(creatorRole)) {
            return true;
        }
        
        // SCOPE_ADMIN只能分配自己被授权的scope
        if ("SCOPE_ADMIN".equals(creatorRole)) {
            if (creatorScopeIds == null || creatorScopeIds.isEmpty()) {
                return false;
            }
            
            try {
                List<String> creatorScopes = objectMapper.readValue(creatorScopeIds, new TypeReference<List<String>>(){});
                // 检查targetScopeIds是否都在creatorScopes中
                return creatorScopes.containsAll(targetScopeIds);
            } catch (Exception e) {
                return false;
            }
        }
        
        return false;
    }
}
