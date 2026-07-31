/*
 * Copyright 2024 OpenJiuWen
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.openjiuwen.memory.authcenter.dto;

import com.openjiuwen.memory.authcenter.domain.User;
import lombok.Getter;
import lombok.Setter;

import java.time.LocalDateTime;

/**
 * 用户信息视图对象（UserVO）
 * 用于向客户端返回脱敏后的用户数据
 * 不包含敏感字段（如密码），仅展示必要业务信息
 */
@Getter
@Setter
public class UserVO {
    private String id;
    /** 用户名 */
    private String username;
    /** 用户角色 */
    private String role;
    /** 权限作用域 ID 列表 */
    private String scopeIds;
    /** 所属租户 ID */
    private String tenantId;
    /** 创建时间戳 */
    private LocalDateTime createdAt;
    /** 最后更新时间戳 */
    private LocalDateTime updatedAt;
    /**
     * 从 User 实体转换为 UserVO
     * 自动过滤 password 等敏感字段，仅保留公开信息
     */
    public static UserVO fromUser(final User user) {
        if (user == null) {
            return null;
        }
        
        final UserVO vo = new UserVO();
        vo.setId(user.getId());
        vo.setUsername(user.getUsername());
        vo.setRole(user.getRole());
        vo.setScopeIds(user.getScopeIds());
        vo.setTenantId(user.getTenantId());
        vo.setCreatedAt(user.getCreatedAt());
        vo.setUpdatedAt(user.getUpdatedAt());
        // 明确排除 password，不进行转换
        return vo;
    }
}
