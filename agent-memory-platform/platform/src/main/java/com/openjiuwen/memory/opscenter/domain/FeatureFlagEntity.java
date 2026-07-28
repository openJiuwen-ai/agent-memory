package com.openjiuwen.memory.opscenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

@Data
@TableName("feature_flag")
public class FeatureFlagEntity {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String tenantId;
    private String scopeId;
    private Boolean enableLongTermMem = true;
    private Boolean enableUserProfile = true;
    private Boolean enableSemanticMemory = true;
    private Boolean enableEpisodicMemory = true;
    private Boolean enableSummaryMemory = true;
    private String customParams;   // JSON
    private Boolean enabled = true;
    private Integer priority = 100;
    private Instant createdAt;
    private Instant updatedAt;
    private String updatedBy;
}
