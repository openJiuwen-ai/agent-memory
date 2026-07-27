package com.openjiuwen.memory.opscenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/** 租户配额（功能6）：上限 + 当前用量（current_* 由 memoryCount 全量翻页降级填充）。 */
@Data
@TableName("tenant_quotas")
public class TenantQuotaEntity {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String adminUserId;
    private Integer maxScopes = 100;
    private Integer maxUsersPerScope = 10000;
    private Integer maxMemoriesPerUser = 100000;
    private Integer maxMessagesPerDay = 1000000;
    private Integer maxStorageMb = 10240;
    private Integer currentScopes = 0;
    private Double currentStorageMb = 0.0;
    private Instant updatedAt;
}
