package com.openjiuwen.memory.configcenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/**
 * 配置审计表 — 2026-07-19 P0-3 v3
 * <p>
 * 操作类型 operation:
 *   TEMPLATE_CREATE / TEMPLATE_UPDATE / TEMPLATE_COPY / TEMPLATE_DELETE /
 *   TEMPLATE_APPLY_TENANT / INSTANCE_CONFIG_UPDATE / INSTANCE_CONFIG_RESTART /
 *   TENANT_CONFIG_UPDATE / SYNC_FROM_TEMPLATE / KERNEL_CONFIG_UPDATE
 * <p>
 * 字段约定：
 * <ul>
 *   <li>{@code operator_id}：操作人（user_id）</li>
 *   <li>{@code tenant_id}：涉及租户（NULL = 平台级）</li>
 *   <li>{@code template_id}：涉及模板</li>
 *   <li>{@code before_value/after_value}：变更前后 JSON（审计可还原）</li>
 *   <li>{@code reason}：操作原因（高危操作必填）</li>
 * </ul>
 */
@Data
@TableName("config_audit_logs")
public class ConfigAuditLogEntity {

    @TableId(type = IdType.ASSIGN_ID)
    private String id;

    @TableField("operator_id")
    private String operatorId;

    @TableField("tenant_id")
    private String tenantId;

    @TableField("template_id")
    private String templateId;

    @TableField("instance_id")
    private String instanceId;

    @TableField("operation")
    private String operation;

    /** 变更前配置 JSON */
    @TableField("before_value")
    private String beforeValue;

    /** 变更后配置 JSON */
    @TableField("after_value")
    private String afterValue;

    @TableField("success")
    private Boolean success;

    @TableField("error_message")
    private String errorMessage;

    @TableField("operated_at")
    private Instant operatedAt;

    @TableField("reason")
    private String reason;
}
