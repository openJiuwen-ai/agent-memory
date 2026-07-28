package com.openjiuwen.memory.opscenter.service.impl;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.opscenter.domain.TaskRegistryEntity;
import com.openjiuwen.memory.opscenter.dto.MigrationRequest;
import com.openjiuwen.memory.opscenter.dto.MigrationResultDTO;
import com.openjiuwen.memory.opscenter.mapper.TaskRegistryMapper;
import com.openjiuwen.memory.opscenter.service.MigrationService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

@Service
public class MigrationServiceImpl implements MigrationService {

    private static final Logger log = LoggerFactory.getLogger(MigrationServiceImpl.class);
    private static final String DEFAULT_TENANT = "default";

    private final MemoryEngineClient client;
    private final TaskRegistryMapper taskMapper;
    private final PermissionChecker permissionChecker;
    private final AuditRecorder auditRecorder;
    private final ObjectMapper objectMapper;

    public MigrationServiceImpl(MemoryEngineClient client,
                                 TaskRegistryMapper taskMapper,
                                 PermissionChecker permissionChecker,
                                 AuditRecorder auditRecorder,
                                 ObjectMapper objectMapper) {
        this.client = client;
        this.taskMapper = taskMapper;
        this.permissionChecker = permissionChecker;
        this.auditRecorder = auditRecorder;
        this.objectMapper = objectMapper;
    }

    @Override
    public MigrationResultDTO migrate(MigrationRequest request, String operator) {
        permissionChecker.check("ops:write");
        Instant startTime = Instant.now();

        // 1. 注册迁移任务到 task_registry
        String taskId = "migration_" + UUID.randomUUID();
        TaskRegistryEntity task = new TaskRegistryEntity();
        task.setId(taskId);
        task.setAdminUserId(DEFAULT_TENANT);
        task.setTaskType("MIGRATION");
        task.setStatus("running");
        task.setTaskConfig(toJson(request));
        task.setStartedAt(startTime);
        task.setCreatedAt(startTime);
        task.setUpdatedAt(startTime);
        taskMapper.insert(task);

        auditRecorder.record(new AuditRecorder.AuditEvent(
                operator, "POST", "/ops/migration", "success", null,
                "数据迁移: " + request.getSourceType() + " → " + request.getTargetType()));

        // 2. 调用内核迁移 API（缺口方法，内核补端点后激活）
        try {
            Object response = client.migrate(request.getSourceConfig(), request.getTargetConfig());

            Instant endTime = Instant.now();
            long durationSeconds = Duration.between(startTime, endTime).getSeconds();

            // 3. 记录迁移结果
            task.setStatus("completed");
            task.setTaskResult(toJson(response));
            task.setStoppedAt(endTime);
            task.setUpdatedAt(endTime);
            taskMapper.updateById(task);

            // 解析内核响应
            int scopeCount = extractScopeCount(response);
            long duration = extractDuration(response, durationSeconds);

            return MigrationResultDTO.builder()
                    .taskId(taskId)
                    .status("completed")
                    .scopeCount(scopeCount)
                    .durationSeconds(duration)
                    .build();
        } catch (Exception e) {
            log.error("Migration failed", e);
            Instant endTime = Instant.now();
            task.setStatus("failed");
            task.setErrorMessage(e.getMessage());
            task.setStoppedAt(endTime);
            task.setUpdatedAt(endTime);
            taskMapper.updateById(task);

            return MigrationResultDTO.builder()
                    .taskId(taskId)
                    .status("failed")
                    .errorMessage(e.getMessage())
                    .durationSeconds(Duration.between(startTime, endTime).getSeconds())
                    .build();
        }
    }

    // —— 内部 ——

    @SuppressWarnings("unchecked")
    private int extractScopeCount(Object response) {
        if (response instanceof Map) {
            Object val = ((Map<String, Object>) response).get("scope_count");
            if (val instanceof Number) {
                return ((Number) val).intValue();
            }
        }
        return 0;
    }

    @SuppressWarnings("unchecked")
    private long extractDuration(Object response, long fallback) {
        if (response instanceof Map) {
            Object val = ((Map<String, Object>) response).get("duration_seconds");
            if (val instanceof Number) {
                return ((Number) val).longValue();
            }
        }
        return fallback;
    }

    private String toJson(Object obj) {
        if (obj == null) return null;
        try {
            return objectMapper.writeValueAsString(obj);
        } catch (Exception e) {
            return obj.toString();
        }
    }
}
