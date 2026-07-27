package com.openjiuwen.memory.opscenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

@Data
@TableName("memory_change_log_snapshot")
public class MemoryChangeLogSnapshotEntity {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String memId;
    private String tenantId;
    private String scopeId;
    private String userId;
    private String changeType;   // CREATE/UPDATE/DELETE
    private String oldContent;
    private String newContent;
    private String operatorId;
    private String requestIp;
    private String reason;
    private String sourceExecutionId;
    private Instant createdAt;
}
