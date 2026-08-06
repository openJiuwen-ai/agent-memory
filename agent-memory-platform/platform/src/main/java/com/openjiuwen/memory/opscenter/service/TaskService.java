package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.opscenter.domain.TaskRegistryEntity;

import java.util.List;
import java.util.Map;

/**
 * 功能8 — 任务管理（Dreaming 启动/停止/状态/列表）。
 * <p>
 * 内核提供 start_dreaming / stop_dreaming / dreaming_status 端点（缺口方法在 MemoryEngineClient 中抛 GapException）。
 * 服务层通过 task_registry 表记录任务生命周期 + 心跳监控。
 */
public interface TaskService {

    /**
     * 启动 Dreaming 任务。
     * 1. 注册任务到 task_registry（status=pending）
     * 2. 调用内核 start_dreaming
     * 3. 更新 status=running, started_at=NOW()
     */
    Map<String, Object> startDreaming(String adminUserId, String scopeId, String userId,
                                       Map<String, Object> config, String operator);

    /**
     * 停止 Dreaming 任务。
     * 1. 调用内核 stop_dreaming
     * 2. 更新 status=stopped, stopped_at=NOW()
     */
    Map<String, Object> stopDreaming(String adminUserId, String scopeId, String userId, String operator);

    /**
     * 查询 Dreaming 状态（透传内核 dreaming_status）。
     */
    Map<String, Object> dreamingStatus(String adminUserId, String scopeId, String userId);

    /**
     * 查询任务列表（按 admin_user_id）。
     */
    List<TaskRegistryEntity> listTasks(String adminUserId);

    /**
     * 查询单个任务详情。
     */
    TaskRegistryEntity getTask(String taskId);
}
