package com.openjiuwen.memory.common.spi;

import com.openjiuwen.memory.common.exception.GapException;

/**
 * 引擎/Scope/Agent/启动配置的读写回内核（属"配置中心"模块）。
 * <p>
 * :8516 未暴露任何配置端点，本模块（功能2 运维参数）的写回统一经此 SPI；
 * 配置中心未接入时抛 {@link GapException}，运维中心仅保留本地草稿。
 */
public interface ConfigCenterClient {

    Object getEngineConfig();

    void updateEngineConfig(Object config);

    default Object getScopeConfig(String scopeId) {
        throw new GapException("配置中心未接入，Scope 配置只读");
    }

    default void updateScopeConfig(String scopeId, Object config) {
        throw new GapException("配置中心未接入，无法写回 Scope 配置");
    }

    default Object getAgentConfig(String scopeId) {
        throw new GapException("配置中心未接入，Agent 策略只读");
    }

    default void updateAgentConfig(String scopeId, Object config) {
        throw new GapException("配置中心未接入，无法写回 Agent 策略");
    }
}
