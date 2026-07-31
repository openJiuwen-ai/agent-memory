package com.openjiuwen.memory.authcenter.dto;

import com.openjiuwen.memory.authcenter.domain.User;
import lombok.Getter;
import lombok.Setter;

import java.time.LocalDateTime;

/**
 * 用户信息视图对象（VO）
 * 用于对外返回用户信息，排除敏感字段（如 password）
 */
@Getter
@Setter
public class UserVO {

    private String id;
    private String username;
    private String role;
    private String scopeIds;
    private String tenantId;
    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;

    /**
     * 从 User 实体对象转换为 UserVO
     */
    public static UserVO fromUser(User user) {
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
