package com.openjiuwen.memory.opscenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

@Data
@TableName("command_execution_log")
public class CommandExecutionLogEntity {

    /** 执行ID，由下发时生成（非自增） */
    @TableId(type = IdType.INPUT)
    private String executionId;
    private String commandCode;
    private String tenantId;
    private String scopeId;
    private String userId;
    private String payloadSnapshot;   // JSON
    private String resultSnapshot;    // JSON
    private String status;            // success/failed/gap/dry_run
    private String gapHint;
    private Integer durationMs;
    private String operatorId;
    private String requestIp;
    private String reason;
    private Instant createdAt;
}
