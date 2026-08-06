package com.openjiuwen.memory.authcenter.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.openjiuwen.memory.authcenter.domain.TokenBlacklist;
import com.openjiuwen.memory.authcenter.mapper.TokenBlacklistMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;

/**
 * Token 黑名单服务：登出的 Token 记入黑名单，直至其自然过期。
 * JWT 过滤器在校验签名/有效期之外，还需检查黑名单。
 */
@Service
public class TokenBlacklistService {

    private static final Logger log = LoggerFactory.getLogger(TokenBlacklistService.class);

    @Autowired
    private TokenBlacklistMapper tokenBlacklistMapper;

    /**
     * 将 Token 加入黑名单
     *
     * @param jti       Token 的 jti（JWT ID）
     * @param token     完整 Token
     * @param username  用户名
     * @param expiresAt Token 过期时间（过期后可清理）
     */
    public void addToBlacklist(String jti, String token, String username, LocalDateTime expiresAt) {
        if (jti == null || jti.isEmpty()) {
            return;
        }
        // 幂等：已存在则跳过（重复调用 logout）
        if (isBlacklisted(jti)) {
            return;
        }
        TokenBlacklist entity = new TokenBlacklist();
        entity.setId(jti);
        entity.setToken(token);
        entity.setUsername(username);
        entity.setExpiresAt(expiresAt);
        entity.setCreatedAt(LocalDateTime.now());
        tokenBlacklistMapper.insert(entity);
    }

    /**
     * 检查 Token 是否在黑名单中
     *
     * @param jti Token 的 jti
     * @return true 表示已登出（黑名单命中）
     */
    public boolean isBlacklisted(String jti) {
        if (jti == null || jti.isEmpty()) {
            return false;
        }
        return tokenBlacklistMapper.selectById(jti) != null;
    }

    /**
     * 定时清理已过期的黑名单记录（每 30 分钟执行一次）。
     * Token 过期后 JWT 校验本身就会拒绝，无需再留在黑名单中。
     */
    @Scheduled(fixedRate = 30 * 60 * 1000)
    public void cleanExpiredTokens() {
        QueryWrapper<TokenBlacklist> wrapper = new QueryWrapper<>();
        wrapper.lt("expires_at", LocalDateTime.now());
        int deleted = tokenBlacklistMapper.delete(wrapper);
        if (deleted > 0) {
            log.info("Token blacklist cleanup: removed {} expired entries", deleted);
        }
    }
}
