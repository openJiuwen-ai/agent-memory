package com.openjiuwen.memory.opscenter.dto;

import lombok.Builder;
import lombok.Data;

/**
 * 数据迁移结果 DTO（§7.7）。
 */
@Data
@Builder
public class MigrationResultDTO {

    private String taskId;

    /** completed / failed */
    private String status;

    private Integer scopeCount;

    private Long durationSeconds;

    private String errorMessage;
}
