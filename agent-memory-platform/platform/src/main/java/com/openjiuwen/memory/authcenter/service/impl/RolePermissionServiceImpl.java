package com.openjiuwen.memory.authcenter.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.openjiuwen.memory.authcenter.domain.RolePermission;
import com.openjiuwen.memory.authcenter.mapper.RolePermissionMapper;
import com.openjiuwen.memory.authcenter.service.RolePermissionService;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.stream.Collectors;

/**
 * 角色权限服务实现类
 */
@Service
public class RolePermissionServiceImpl extends ServiceImpl<RolePermissionMapper, RolePermission> implements RolePermissionService {
    
    @Override
    public List<String> getPermissionsByRole(String role) {
        return baseMapper.selectList(
            new LambdaQueryWrapper<RolePermission>().eq(RolePermission::getRole, role)
        ).stream()
        .map(RolePermission::getPermission)
        .collect(Collectors.toList());
    }
}
