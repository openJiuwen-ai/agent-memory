package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.dto.RawResponses;
import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.opscenter.domain.TaskRegistryEntity;
import com.openjiuwen.memory.opscenter.mapper.TaskRegistryMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * 功能8 — 任务心跳监控（§7.6.2）。
 * <p>
 * 每 30 秒检查 running 状态的 DREAMING 任务，通过内核健康检查间接判断内核是否存活。
 * 内核不可达 → 标记任务为 failed，记录审计日志。
 */
@Component
public class TaskHeartbeatMonitor {

    private static final Logger log = LoggerFactory.getLogger(TaskHeartbeatMonitor.class);

    private final MemoryEngineClient client;
    private final TaskRegistryMapper taskMapper;
    private final AuditRecorder auditRecorder;

    public TaskHeartbeatMonitor(MemoryEngineClient client,
                                TaskRegistryMapper taskMapper,
                                AuditRecorder auditRecorder) {
        this.client = client;
        this.taskMapper = taskMapper;
        this.auditRecorder = auditRecorder;
    }

    /**
     * 每 30 秒执行一次心跳检查。
     */
    @Scheduled(fixedRate = 30000)
    public void monitorTasks() {
        List<TaskRegistryEntity> runningTasks;
        try {
            runningTasks = taskMapper.findByStatusAndTaskType("running", "DREAMING");
        } catch (Exception e) {
            log.error("Failed to query running tasks", e);
            return;
        }
        if (runningTasks == null || runningTasks.isEmpty()) {
            return;
        }

        boolean kernelHealthy = isKernelHealthy();

        for (TaskRegistryEntity task : runningTasks) {
            try {
                if (kernelHealthy) {
                    // 更新心跳
                    task.setLastHeartbeat(Instant.now());
                    task.setUpdatedAt(Instant.now());
                    taskMapper.updateById(task);
                } else {
                    // 内核不可用，标记任务为异常
                    task.setStatus("failed");
                    task.setStoppedAt(Instant.now());
                    task.setErrorMessage("Kernel unavailable");
                    task.setUpdatedAt(Instant.now());
                    taskMapper.updateById(task);

                    auditRecorder.record(new AuditRecorder.AuditEvent(
                            "SYSTEM", "DREAMING_AUTO_STOP",
                            "/ops/tasks/" + task.getId(), "failed", null,
                            "Kernel unavailable, auto-stop task: " + task.getId()));
                }
            } catch (Exception e) {
                log.error("Heartbeat check failed for task: {}", task.getId(), e);
            }
        }
    }

    /**
     * 通过内核健康检查 API 间接判断内核是否存活。
     */
    private boolean isKernelHealthy() {
        try {
            RawResponses.Health health = client.health();
            return health != null && "healthy".equalsIgnoreCase(health.getStatus());
        } catch (Exception e) {
            log.warn("Kernel health check failed: {}", e.getMessage());
            return false;
        }
    }
}
