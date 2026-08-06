package com.openjiuwen.memory.configcenter.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.configcenter.constant.KernelConfigConstants;
import com.openjiuwen.memory.configcenter.domain.ConfigAuditLogEntity;
import com.openjiuwen.memory.configcenter.dto.KernelConfigUpdateRequest;
import com.openjiuwen.memory.configcenter.dto.KernelConfigUpdateResultDTO;
import com.openjiuwen.memory.configcenter.mapper.ConfigAuditLogMapper;
import com.openjiuwen.memory.configcenter.service.KernelConfigService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * 内核配置管理服务实现 — Push 模型。
 * <p>
 * 遵循设计文档 §5.3 Push 模型：
 * <ul>
 *   <li>GET：代理调用内核 GET /admin/config，敏感字段脱敏展示</li>
 *   <li>PUT：过滤只读参数 → 内核 PUT /admin/config 写入 → POST /admin/restart 重启</li>
 * </ul>
 * <p>
 * 核心原则：内核是唯一配置源，服务层是内核的"远程编辑器"。
 * 服务层 DB 中不保留任何内核配置数据。
 * <p>
 * 2026-07-19 P0-3 v3：内核配置页只读展示安装参数 + 连接参数（全局默认值）。
 * 可修改参数拆分为热启动模板（tpl_instance_hot，立即生效）与冷启动模板
 * （tpl_instance_cold，需重启），均由 ConfigTemplateService 管理。
 * <p>
 * 内核 API（getKernelConfig/pushKernelConfig/restartKernel）已在 DefaultMemoryEngineClient 中覆写，
 * 通过 RestClient 调用内核 GET/PUT /admin/config 及 POST /admin/restart 端点。
 */
@Service
public class KernelConfigServiceImpl implements KernelConfigService {

    private static final Logger log = LoggerFactory.getLogger(KernelConfigServiceImpl.class);

    // P2-2: READONLY_PARAMS 已提取为共享常量 KernelConfigConstants.READONLY_PARAMS，
    // InstanceConfigServiceImpl 和 KernelConfigServiceImpl 共用，确保所有 Push 路径统一过滤。

    private final MemoryEngineClient client;
    private final ConfigAuditLogMapper auditMapper;
    private final ObjectMapper objectMapper;

    public KernelConfigServiceImpl(MemoryEngineClient client, ConfigAuditLogMapper auditMapper,
                                   ObjectMapper objectMapper) {
        this.client = client;
        this.auditMapper = auditMapper;
        this.objectMapper = objectMapper;
    }

    // —— GET：获取内核配置（脱敏展示）§5.3.4 ——

    @Override
    public Map<String, Object> getKernelConfig() {
        // 代理调用内核 GET /admin/config
        Map<String, Object> kernelConfig;
        try {
            kernelConfig = client.getKernelConfig();
        } catch (Exception e) {
            // 内核未暴露 API（GapException）— 返回空结构 + 错误标记
            log.debug("getKernelConfig 内核调用失败: {}", e.getMessage());
            Map<String, Object> fallback = new LinkedHashMap<>();
            fallback.put("runtime", Collections.emptyMap());
            fallback.put("storage", Collections.emptyMap());
            fallback.put("vector_engine", Collections.emptyMap());
            fallback.put("engine", Collections.emptyMap());
            fallback.put("restart_required", false);
            fallback.put("source", "kernel");
            fallback.put("available", false);
            fallback.put("error", "内核配置查询 API 未就绪: " + e.getMessage());
            return fallback;
        }

        // 内核返回的配置已经是脱敏 + 分类结构，直接透传
        // 补充元信息
        if (!kernelConfig.containsKey("source")) {
            kernelConfig.put("source", "kernel");
        }
        kernelConfig.put("available", true);
        return kernelConfig;
    }

    // —— PUT：Push 配置到内核 + 重启 §5.3.4 ——

    @Override
    public KernelConfigUpdateResultDTO updateKernelConfig(KernelConfigUpdateRequest request, String operator) {
        Map<String, String> updates = request.getUpdates();
        if (updates == null || updates.isEmpty()) {
            throw new BizException(ResultCode.BAD_REQUEST, "updates 不能为空");
        }

        // 1. 过滤只读参数 — 拒绝修改（安装参数 + 连接参数均只读展示）
        Map<String, String> accepted = new LinkedHashMap<>();
        List<String> rejected = new ArrayList<>();
        for (Map.Entry<String, String> entry : updates.entrySet()) {
            String upperKey = entry.getKey().toUpperCase();
            if (KernelConfigConstants.isReadonly(upperKey)) {
                rejected.add(entry.getKey());
            } else {
                accepted.put(upperKey, entry.getValue());
            }
        }

        if (accepted.isEmpty()) {
            throw new BizException(ResultCode.BAD_REQUEST,
                    "所有参数均为只读参数（安装参数/连接参数），不可通过 Push 修改。可修改参数请到配置模板的热启动/冷启动模板中编辑");
        }

        // 2. 调用内核 PUT /admin/config — 内核写入配置
        boolean pushSuccess = false;
        String pushError = null;
        try {
            client.pushKernelConfig(accepted);
            pushSuccess = true;
        } catch (Exception e) {
            pushError = e.getMessage();
            log.warn("pushKernelConfig 内核调用失败: {}", pushError);
        }

        // 3. 触发内核重启（若 restart=true 且 Push 成功）
        boolean restartTriggered = false;
        String restartStatus = "skipped";
        if (request.isRestart() && pushSuccess) {
            try {
                client.restartKernel();
                restartTriggered = true;
                restartStatus = "in_progress";
            } catch (Exception e) {
                restartStatus = "failed: " + e.getMessage();
                log.warn("restartKernel 内核调用失败: {}", e.getMessage());
            }
        }

        // 4. 记录审计日志
        recordKernelAudit(operator, accepted, rejected, request.getReason(),
                pushSuccess, pushError, restartTriggered, restartStatus);

        // 5. 构造返回结果
        String message;
        if (!pushSuccess) {
            message = "配置 Push 失败: " + pushError;
        } else if (restartTriggered) {
            message = "配置已写入内核，正在重启内核服务...";
        } else if (request.isRestart()) {
            message = "配置已写入内核，但重启失败: " + restartStatus;
        } else {
            message = "配置已写入内核（未触发重启，需手动重启生效）";
        }

        return KernelConfigUpdateResultDTO.builder()
                .updatedKeys(new ArrayList<>(accepted.keySet()))
                .rejectedKeys(rejected)
                .restartTriggered(restartTriggered)
                .restartStatus(restartStatus)
                .message(message)
                .build();
    }

    // —— 内部工具 ——

    private void recordKernelAudit(String operator, Map<String, String> accepted,
                                    List<String> rejected, String reason,
                                    boolean success, String errorMsg,
                                    boolean restartTriggered, String restartStatus) {
        ConfigAuditLogEntity audit = new ConfigAuditLogEntity();
        audit.setId(UUID.randomUUID().toString());
        audit.setOperatorId(operator); // 使用实际操作人，替代硬编码 "default"
        audit.setTenantId(null); // 内核级配置，无租户
        audit.setInstanceId("default");
        audit.setOperation("KERNEL_CONFIG_UPDATE");
        // before_value 留空（服务层不存储内核配置副本）
        audit.setBeforeValue(null);
        // after_value 记录已接受的 key 列表（不记录 value，敏感信息不落库）
        audit.setAfterValue(toJsonString(Map.of(
                "accepted_keys", accepted.keySet(),
                "rejected_keys", rejected,
                "reason", reason != null ? reason : "",
                "restart_triggered", restartTriggered,
                "restart_status", restartStatus
        )));
        audit.setSuccess(success);
        audit.setErrorMessage(errorMsg);
        audit.setReason(reason);
        audit.setOperatedAt(Instant.now());
        auditMapper.insert(audit);
    }

    private String toJsonString(Object obj) {
        try {
            return objectMapper.writeValueAsString(obj);
        } catch (Exception e) {
            return "{}";
        }
    }
}
