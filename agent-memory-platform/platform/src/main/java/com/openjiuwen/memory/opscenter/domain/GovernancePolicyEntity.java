package com.openjiuwen.memory.opscenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/** 治理策略（功能6）：四类 LIFECYCLE/QUALITY/QUOTA/COMPLIANCE；admin_user_id NULL=全局。 */
@Data
@TableName("governance_policies")
public class GovernancePolicyEntity {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** NULL=全局策略 */
    private String adminUserId;
    private String policyType;
    private String policyName;
    /** JSON */
    private String policyConfig;
    private Boolean isEnabled = true;
    private String createdBy;
    private Instant createdAt;
    private Instant updatedAt;
}
