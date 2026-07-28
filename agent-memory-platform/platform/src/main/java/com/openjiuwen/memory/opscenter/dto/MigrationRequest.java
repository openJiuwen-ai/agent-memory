package com.openjiuwen.memory.opscenter.dto;

import lombok.Data;

import java.util.Map;

/**
 * 数据迁移请求 DTO（§7.7）。
 * <p>
 * 调用内核 migrate_between_indices HTTP 端点，在两个 BaseMemoryIndex 实例之间批量迁移数据。
 * 迁移是批量复制，源数据保留。迁移过程中服务不可用（730 阶段无在线迁移）。
 */
@Data
public class MigrationRequest {

    /** 源存储类型：chroma / milvus / elasticsearch / gauss_db 等 */
    private String sourceType;

    /** 源存储配置（如 persist_dir / milvus_uri 等） */
    private Map<String, Object> sourceConfig;

    /** 目标存储类型 */
    private String targetType;

    /** 目标存储配置 */
    private Map<String, Object> targetConfig;
}
