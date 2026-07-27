package com.openjiuwen.memory.opscenter.service.impl;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.opscenter.domain.TaskRegistryEntity;
import com.openjiuwen.memory.opscenter.mapper.TaskRegistryMapper;
import com.openjiuwen.memory.opscenter.service.TaskService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
public class TaskServiceImpl implements TaskService {

    private static final Logger log = LoggerFactory.getLogger(TaskServiceImpl.class);
    private static final String DEFAULT_TENANT = "default";

    private final TaskRegistryMapper taskMapper;
    private final MemoryEngineClient client;
    private final PermissionChecker permissionChecker;
    private final AuditRecorder auditRecorder;
    private final ObjectMapper objectMapper;

    public TaskServiceImpl(TaskRegistryMapper taskMapper,
                           MemoryEngineClient client,
                           PermissionChecker permissionChecker,
                           AuditRecorder auditRecorder,
                           ObjectMapper objectMapper) {
        this.taskMapper = taskMapper;
        this.client = client;
        this.permissionChecker = permissionChecker;
        this.auditRecorder = auditRecorder;
        this.objectMapper = objectMapper;
    }

    @Override
    public Map<String, Object> startDreaming(String adminUserId, String scopeId, String userId,
                                              Map<String, Object> config, String operator) {
        permissionChecker.check("ops:write");
        String tenant = resolveTenant(adminUserId);
        Instant now = Instant.now();

        // 1. 注册任务到 task_registry
        TaskRegistryEntity task = new TaskRegistryEntity();
        task.setId("dreaming_" + UUID.randomUUID());
        task.setAdminUserId(tenant);
        task.setTaskType("DREAMING");
        task.setScopeId(scopeId);
        task.setUserId(userId);
        task.setStatus("pending");
        task.setTaskConfig(toJson(config));
        task.setCreatedAt(now);
        task.setUpdatedAt(now);
        taskMapper.insert(task);

        // 2. 调用内核 start_dreaming（缺口方法，内核补端点后激活）
        try {
            Object result = client.startDreaming(buildDreamingConfig(scopeId, userId, config));

            // 3. 更新 status=running
            task.setStatus("running");
            task.setStartedAt(Instant.now());
            task.setTaskResult(toJson(result));
            task.setUpdatedAt(Instant.now());
            taskMapper.updateById(task);
        } catch (Exception e) {
            // 内核不可达或端点未暴露 → 标记 failed
            task.setStatus("failed");
            task.setErrorMessage(e.getMessage());
            task.setStoppedAt(Instant.now());
            task.setUpdatedAt(Instant.now());
            taskMapper.updateById(task);
            throw e;
        }

        auditRecorder.record(new AuditRecorder.AuditEvent(
                operator, "POST", "/ops/tasks/dreaming/start", "success", null,
                "启动 Dreaming: scope=" + scopeId));

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("task_id", task.getId());
        out.put("status", task.getStatus());
        out.put("started_at", task.getStartedAt() != null ? task.getStartedAt().toString() : null);
        return out;
    }

    @Override
    public Map<String, Object> stopDreaming(String adminUserId, String scopeId, String userId, String operator) {
        permissionChecker.check("ops:write");
        String tenant = resolveTenant(adminUserId);

        // 调用内核 stop_dreaming（缺口方法）
        Object result = client.stopDreaming(scopeId, userId);

        // 更新 task_registry 中 running 的 DREAMING 任务为 stopped（SQL 层过滤租户）
        List<TaskRegistryEntity> runningTasks = taskMapper.findByAdminUserIdAndStatusAndTaskType(tenant, "running", "DREAMING");
        for (TaskRegistryEntity task : runningTasks) {
            task.setStatus("stopped");
            task.setStoppedAt(Instant.now());
            task.setTaskResult(toJson(result));
            task.setUpdatedAt(Instant.now());
            taskMapper.updateById(task);
        }

        auditRecorder.record(new AuditRecorder.AuditEvent(
                operator, "POST", "/ops/tasks/dreaming/stop", "success", null,
                "停止 Dreaming: scope=" + scopeId));

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", "stopped");
        out.put("stopped_at", Instant.now().toString());
        return out;
    }

    @Override
    public Map<String, Object> dreamingStatus(String adminUserId, String scopeId, String userId) {
        permissionChecker.check("ops:read");
        // 透传内核 dreaming_status（缺口方法）
        Object status = client.dreamingStatus();
        if (status instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> map = (Map<String, Object>) status;
            return map;
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", status != null ? status.toString() : "unknown");
        return out;
    }

    @Override
    public List<TaskRegistryEntity> listTasks(String adminUserId) {
        permissionChecker.check("ops:read");
        String tenant = resolveTenant(adminUserId);
        return taskMapper.findByAdminUserIdOrderByCreatedAtDesc(tenant);
    }

    @Override
    public TaskRegistryEntity getTask(String taskId) {
        permissionChecker.check("ops:read");
        return taskMapper.selectById(taskId);
    }

    // —— 内部 ——

    private String resolveTenant(String adminUserId) {
        return adminUserId == null || adminUserId.isBlank() ? DEFAULT_TENANT : adminUserId;
    }

    private Map<String, Object> buildDreamingConfig(String scopeId, String userId, Map<String, Object> config) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("scope_id", scopeId != null ? scopeId : "__default__");
        out.put("user_id", userId != null ? userId : "__default__");
        if (config != null) {
            out.put("enabled", true);
            out.putAll(config);
        } else {
            out.put("enabled", true);
            out.put("interval_seconds", 14400);
            out.put("min_session_rounds", 4);
        }
        return out;
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
