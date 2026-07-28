package com.openjiuwen.memory.opscenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/** 任务注册表（功能8/9）：Dreaming / Migration 等后台任务的生命周期记录。 */
@Data
@TableName("task_registry")
public class TaskRegistryEntity {

    @TableId(type = IdType.INPUT)
    private String id;

    private String adminUserId;

    /** DREAMING / MIGRATION */
    private String taskType;

    private String scopeId;

    private String userId;

    /** pending/running/stopped/failed/completed */
    private String status;

    /** JSON：任务配置 */
    private String taskConfig;

    /** JSON：任务结果 */
    private String taskResult;

    private String errorMessage;

    private Instant startedAt;

    private Instant stoppedAt;

    private Instant lastHeartbeat;

    private Instant createdAt;

    private Instant updatedAt;
}
