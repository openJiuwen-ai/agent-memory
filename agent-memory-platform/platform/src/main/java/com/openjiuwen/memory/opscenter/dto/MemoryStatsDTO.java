package com.openjiuwen.memory.opscenter.dto;

import lombok.Builder;
import lombok.Data;

import java.util.List;

/**
 * 记忆统计仪表盘数据（§7.5.2）。
 * <p>
 * 服务层增量价值：内核 user_mem_total_num 只返回单用户单 scope 总数，
 * 服务层跨 scope 聚合、按维度统计、从变更快照/消息日志聚合趋势。
 */
@Data
@Builder
public class MemoryStatsDTO {

    /** 按类型统计 */
    private List<TypeStats> byType;

    /** 按 Scope 统计 */
    private List<ScopeStats> byScope;

    /** 记忆增长趋势（最近 N 天） */
    private List<GrowthTrend> growthTrend;

    /** 按用户统计 Top10 */
    private List<UserStats> topUsers;

    /** 存储用量 */
    private StorageUsage storage;

    /** 汇总卡片 */
    private SummaryCards summary;

    @Data
    @Builder
    public static class TypeStats {
        private String type;
        private long count;
        private double percentage;
    }

    @Data
    @Builder
    public static class ScopeStats {
        private String scopeId;
        private long count;
    }

    @Data
    @Builder
    public static class GrowthTrend {
        private String date;
        private long created;
        private long updated;
        private long deleted;
        private long netGrowth;
    }

    @Data
    @Builder
    public static class UserStats {
        private String userId;
        private long messageCount;
    }

    @Data
    @Builder
    public static class StorageUsage {
        private double usedMb;
        private double maxMb;
        private double percentage;
    }

    @Data
    @Builder
    public static class SummaryCards {
        private long totalMemories;
        private long weekCreated;
        private long weekUpdated;
        private long weekDeleted;
    }
}
