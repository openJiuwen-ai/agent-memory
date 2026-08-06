package com.openjiuwen.memory.configcenter.service;

import com.openjiuwen.memory.configcenter.domain.InstanceConfigEntity;
import com.openjiuwen.memory.configcenter.dto.InstanceConfigDTO;

/**
 * 实例级配置服务 — 2026-07-17 P0-3 v2 重构
 * <p>
 * 单例 (id=1)。修改时提示需重启实例。
 */
public interface InstanceConfigService {

    /** 获取实例级配置 */
    InstanceConfigEntity get();

    /** 修改实例级配置（自动 version +1） */
    InstanceConfigDTO update(String configJson, String operator);
}
