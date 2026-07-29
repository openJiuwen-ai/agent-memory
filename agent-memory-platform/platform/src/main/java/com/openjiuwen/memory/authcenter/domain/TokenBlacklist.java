package com.openjiuwen.memory.authcenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * Token 黑名单实体类（用于 JWT 登出失效）
 */
@Data
@TableName("token_blacklist")
public class TokenBlacklist {

    /**
     * Token 的 jti（JWT ID）
     */
    @TableId(type = IdType.INPUT)
    private String id;

    /**
     * 完整 Token（可选，用于调试）
     */
    private String token;

    /**
     * 用户名
     */
    private String username;

    /**
     * Token 过期时间
     */
    private LocalDateTime expiresAt;

    /**
     * 创建时间
     */
    private LocalDateTime createdAt;
}
