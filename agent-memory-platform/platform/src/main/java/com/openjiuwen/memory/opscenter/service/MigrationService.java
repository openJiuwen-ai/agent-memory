package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.opscenter.dto.MigrationRequest;
import com.openjiuwen.memory.opscenter.dto.MigrationResultDTO;

/**
 * 功能9 — 数据迁移服务（§7.7）。
 * <p>
 * 调用内核 migrate_between_indices HTTP 端点，在两个 BaseMemoryIndex 实例之间批量迁移数据。
 * 迁移是批量复制，源数据保留。迁移过程中服务不可用（730 阶段无在线迁移）。
 */
public interface MigrationService {

    /**
     * 执行数据迁移。
     * 1. 注册迁移任务到 task_registry（status=running）
     * 2. 调用内核 POST /migrate_between_indices
     * 3. 记录迁移结果到 task_registry
     */
    MigrationResultDTO migrate(MigrationRequest request, String operator);
}
