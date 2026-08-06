package com.openjiuwen.memory.configcenter.service.impl;

import com.openjiuwen.memory.common.spi.ConfirmTokenService;
import com.openjiuwen.memory.configcenter.domain.ConfirmTokenEntity;
import com.openjiuwen.memory.configcenter.mapper.ConfirmTokenMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;

/**
 * 二次确认服务 DB-backed 实现（P0-2 流程层）— 替换 SPI 默认 noop。
 * <p>
 * 通过 SPI 覆盖：{@code SpiDefaults} 中的 noop ConfirmTokenService 会被此实现替代。
 * 关键点：
 * <ul>
 *   <li>token：UUID，应用层生成</li>
 *   <li>TTL：5 分钟（{@link #DEFAULT_TTL_MINUTES}）</li>
 *   <li>防重放：consume 时原子 update consumed=0→1，失败说明已被消费</li>
 * </ul>
 * <p>
 * payload 字段：当前 SPI 接口未传 payload；如需传递上下文可在 issue 端用 {@code "action:resource"} 编码。
 */
@Service
@Primary
@ConditionalOnMissingBean(name = "confirmTokenServiceOverride")
public class SpiConfirmTokenServiceImpl implements ConfirmTokenService {

    private static final Logger log = LoggerFactory.getLogger(SpiConfirmTokenServiceImpl.class);
    private static final int DEFAULT_TTL_MINUTES = 5;

    private final ConfirmTokenMapper mapper;

    public SpiConfirmTokenServiceImpl(ConfirmTokenMapper mapper) {
        this.mapper = mapper;
    }

    @Override
    @Transactional
    public String issue(String operator, String action, String resource) {
        ConfirmTokenEntity token = new ConfirmTokenEntity();
        token.setToken(UUID.randomUUID().toString());
        token.setOperatorId(operator);
        token.setAction(action);
        token.setResource(resource);
        token.setPayload(null);
        token.setExpiresAt(Instant.now().plus(DEFAULT_TTL_MINUTES, ChronoUnit.MINUTES));
        token.setConsumed(false);
        token.setIssuedAt(Instant.now());
        mapper.insert(token);
        log.info("二次确认令牌已签发: operator={}, action={}, resource={}, expires_at={}",
                operator, action, resource, token.getExpiresAt());
        return token.getToken();
    }

    @Override
    public boolean validate(String token, String operator, String action, String resource) {
        if (token == null || token.isBlank()) {
            return false;
        }
        ConfirmTokenEntity entity = mapper.findByToken(token);
        if (entity == null) {
            return false;
        }
        if (!entity.getOperatorId().equals(operator)) return false;
        if (!entity.getAction().equals(action)) return false;
        if (!entity.getResource().equals(resource)) return false;
        if (entity.getExpiresAt().isBefore(Instant.now())) return false;
        return !Boolean.TRUE.equals(entity.getConsumed());
    }

    @Override
    @Transactional
    public void consume(String token) {
        if (token == null || token.isBlank()) return;
        int rows = mapper.markConsumed(token);
        if (rows > 0) {
            log.info("二次确认令牌已消费: token={}", token);
        } else {
            log.warn("二次确认令牌消费失败（可能已消费或不存在）: token={}", token);
        }
    }
}
