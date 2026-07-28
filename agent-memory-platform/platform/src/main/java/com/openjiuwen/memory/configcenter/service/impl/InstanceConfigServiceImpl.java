package com.openjiuwen.memory.configcenter.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.configcenter.constant.KernelConfigConstants;
import com.openjiuwen.memory.configcenter.domain.ConfigAuditLogEntity;
import com.openjiuwen.memory.configcenter.domain.InstanceConfigEntity;
import com.openjiuwen.memory.configcenter.dto.InstanceConfigDTO;
import com.openjiuwen.memory.configcenter.mapper.ConfigAuditLogMapper;
import com.openjiuwen.memory.configcenter.mapper.InstanceConfigMapper;
import com.openjiuwen.memory.configcenter.service.InstanceConfigService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * 实例级配置服务实现 — 2026-07-17 P0-3 v2
 */
@Service
public class InstanceConfigServiceImpl implements InstanceConfigService {

    private static final Logger log = LoggerFactory.getLogger(InstanceConfigServiceImpl.class);

    private final InstanceConfigMapper instanceConfigMapper;
    private final ConfigAuditLogMapper auditLogMapper;
    private final MemoryEngineClient memoryEngineClient;
    private final ObjectMapper objectMapper;

    public InstanceConfigServiceImpl(InstanceConfigMapper instanceConfigMapper,
                                      ConfigAuditLogMapper auditLogMapper,
                                      MemoryEngineClient memoryEngineClient,
                                      ObjectMapper objectMapper) {
        this.instanceConfigMapper = instanceConfigMapper;
        this.auditLogMapper = auditLogMapper;
        this.memoryEngineClient = memoryEngineClient;
        this.objectMapper = objectMapper;
    }

    @Override
    public InstanceConfigEntity get() {
        InstanceConfigEntity entity = instanceConfigMapper.selectById(1);
        if (entity == null) {
            // 不存在则懒加载
            entity = new InstanceConfigEntity();
            entity.setId(1);
            entity.setVersion(0);
        }
        return entity;
    }

    @Override
    @Transactional
    public InstanceConfigDTO update(String configJson, String operator) {
        InstanceConfigEntity entity = instanceConfigMapper.selectById(1);
        String before = entity != null ? entity.getConfigJson() : null;
        if (entity == null) {
            entity = new InstanceConfigEntity();
            entity.setId(1);
            entity.setVersion(1);
        } else {
            entity.setVersion(entity.getVersion() + 1);
        }
        entity.setConfigJson(configJson);
        entity.setUpdatedAt(Instant.now());
        entity.setUpdatedBy(operator);
        instanceConfigMapper.insertOrUpdate(entity);
        memoryEngineClient.pushKernelConfig(parseInstanceConfigJson(configJson));

        ConfigAuditLogEntity audit = new ConfigAuditLogEntity();
        audit.setId(UUID.randomUUID().toString());
        audit.setOperatorId(operator);
        audit.setInstanceId("default");
        audit.setOperation("INSTANCE_CONFIG_UPDATE");
        audit.setBeforeValue(before);
        audit.setAfterValue(configJson);
        audit.setSuccess(true);
        audit.setOperatedAt(Instant.now());
        auditLogMapper.insert(audit);

        return toDTO(entity);
    }

    private InstanceConfigDTO toDTO(InstanceConfigEntity e) {
        InstanceConfigDTO dto = new InstanceConfigDTO();
        dto.setTemplateId(e.getTemplateId());
        dto.setConfigJson(e.getConfigJson());
        dto.setVersion(e.getVersion());
        dto.setUpdatedAt(e.getUpdatedAt() != null ? e.getUpdatedAt().toString() : null);
        dto.setUpdatedBy(e.getUpdatedBy());
        return dto;
    }

    @SuppressWarnings("unchecked")
    private Map<String, String> parseInstanceConfigJson(String configJson) {
        try {
            Map<String, Object> raw = objectMapper.readValue(configJson, Map.class);
            Map<String, String> updates = new LinkedHashMap<>();
            List<String> rejected = new ArrayList<>();
            // P2-2 Fix: 过滤只读参数，与 KernelConfigServiceImpl 保持一致
            // 安装参数（IP/PORT/数据目录等）和连接参数（API_KEY 等）不可通过 Push 修改
            for (Map.Entry<String, Object> entry : raw.entrySet()) {
                String key = entry.getKey();
                if (KernelConfigConstants.isReadonly(key)) {
                    rejected.add(key);
                } else {
                    updates.put(key, entry.getValue() == null ? "" : String.valueOf(entry.getValue()));
                }
            }
            if (!rejected.isEmpty()) {
                log.warn("InstanceConfig push filtered readonly params: {}", rejected);
            }
            return updates;
        } catch (Exception e) {
            throw new BizException(ResultCode.BAD_REQUEST, "实例配置不是合法 JSON: " + e.getMessage());
        }
    }
}
