package com.openjiuwen.memory.opscenter.service.impl;

import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.dto.GetUserMemByPageRequest;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.client.dto.MemoryType;
import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.logcenter.service.MessageLogService;
import com.openjiuwen.memory.opscenter.domain.MemoryChangeLogSnapshotEntity;
import com.openjiuwen.memory.opscenter.domain.TenantQuotaEntity;
import com.openjiuwen.memory.opscenter.dto.MemoryStatsDTO;
import com.openjiuwen.memory.opscenter.mapper.MemoryChangeLogSnapshotMapper;
import com.openjiuwen.memory.opscenter.mapper.TenantQuotaMapper;
import com.openjiuwen.memory.opscenter.service.MemoryStatsService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.*;

/**
 * 记忆统计仪表盘服务实现（§7.5.2）。
 * <p>
 * 数据来源：
 * <ul>
 *   <li>按类型/Scope 统计：通过内核 HTTP 逐 scope 全量翻页降级计数（:8516 无全局总数接口）</li>
 *   <li>增长趋势：从 memory_change_log_snapshot 聚合（纯服务层数据）</li>
 *   <li>按用户 Top10：经 MessageLogService 代理内核 KR-MSG（V3 §6.6 整改，不再读 request_response_logs 死表）</li>
 *   <li>存储用量：从 tenant_quotas 读取</li>
 * </ul>
 */
@Service
public class MemoryStatsServiceImpl implements MemoryStatsService {

    private static final Logger log = LoggerFactory.getLogger(MemoryStatsServiceImpl.class);

    private static final List<String> MEMORY_TYPES = List.of(
            MemoryType.USER_PROFILE,
            MemoryType.SEMANTIC_MEMORY,
            MemoryType.EPISODIC_MEMORY,
            MemoryType.SUMMARY,
            MemoryType.VARIABLE,
            MemoryType.MIDDLE_TERM_MEMORY
    );

    private static final int GROWTH_TREND_DAYS = 7;
    private static final int TOP_USERS_LIMIT = 10;

    private final MemoryEngineClient client;
    private final MemoryChangeLogSnapshotMapper changeLogMapper;
    private final MessageLogService messageLogService;
    private final TenantQuotaMapper quotaMapper;
    private final PermissionChecker permissionChecker;

    public MemoryStatsServiceImpl(MemoryEngineClient client,
                                  MemoryChangeLogSnapshotMapper changeLogMapper,
                                  MessageLogService messageLogService,
                                  TenantQuotaMapper quotaMapper,
                                  PermissionChecker permissionChecker) {
        this.client = client;
        this.changeLogMapper = changeLogMapper;
        this.messageLogService = messageLogService;
        this.quotaMapper = quotaMapper;
        this.permissionChecker = permissionChecker;
    }

    @Override
    public MemoryStatsDTO getMemoryStats(String adminUserId, String scopeId, String userId) {
        permissionChecker.check("memory:read");

        String effectiveScope = (scopeId == null || scopeId.isBlank()) ? "__default__" : scopeId;
        String effectiveUser = (userId == null || userId.isBlank()) ? "__default__" : userId;

        // 1. 按类型统计 + 总记忆数（通过内核 HTTP 逐类型计数）
        List<MemoryStatsDTO.TypeStats> byType = new ArrayList<>();
        long totalMemories = 0;
        for (String type : MEMORY_TYPES) {
            long count = countMemoriesByType(effectiveUser, effectiveScope, type);
            if (count > 0) {
                byType.add(MemoryStatsDTO.TypeStats.builder()
                        .type(type)
                        .count(count)
                        .build());
                totalMemories += count;
            }
        }
        // 计算百分比
        final long total = totalMemories;
        for (MemoryStatsDTO.TypeStats ts : byType) {
            ts.setPercentage(total > 0 ? Math.round(ts.getCount() * 1000.0 / total) / 10.0 : 0.0);
        }

        // 2. 按 Scope 统计（单 scope 查询时只返回自身）
        List<MemoryStatsDTO.ScopeStats> byScope = List.of(
                MemoryStatsDTO.ScopeStats.builder()
                        .scopeId(effectiveScope)
                        .count(totalMemories)
                        .build()
        );

        // 3. 记忆增长趋势（最近7天，从 memory_change_log_snapshot 聚合）
        List<MemoryStatsDTO.GrowthTrend> growthTrend = buildGrowthTrend();

        // 4. 按用户统计 Top10（从 request_response_logs 聚合）
        List<MemoryStatsDTO.UserStats> topUsers = buildTopUsers(adminUserId);

        // 5. 存储用量
        MemoryStatsDTO.StorageUsage storage = buildStorageUsage(adminUserId);

        // 6. 汇总卡片
        Instant weekAgo = Instant.now().minus(java.time.Duration.ofDays(7));
        long weekCreated = changeLogMapper.countByChangeTypeAndCreatedAtAfter("CREATE", weekAgo);
        long weekUpdated = changeLogMapper.countByChangeTypeAndCreatedAtAfter("UPDATE", weekAgo);
        long weekDeleted = changeLogMapper.countByChangeTypeAndCreatedAtAfter("DELETE", weekAgo);

        MemoryStatsDTO.SummaryCards summary = MemoryStatsDTO.SummaryCards.builder()
                .totalMemories(totalMemories)
                .weekCreated(weekCreated)
                .weekUpdated(weekUpdated)
                .weekDeleted(weekDeleted)
                .build();

        return MemoryStatsDTO.builder()
                .byType(byType)
                .byScope(byScope)
                .growthTrend(growthTrend)
                .topUsers(topUsers)
                .storage(storage)
                .summary(summary)
                .build();
    }

    /**
     * 通过内核 HTTP 全量翻页降级计数指定类型的记忆数。
     * :8516 的 total=当前页条数，不可信，降级为全量翻页累加。
     */
    private long countMemoriesByType(String userId, String scopeId, String memoryType) {
        try {
            int pageSize = 100;
            int maxPages = 10;
            long count = 0;
            int pageIdx = 1;
            while (pageIdx <= maxPages) {
                GetUserMemByPageRequest req = new GetUserMemByPageRequest();
                req.setUserId(userId);
                req.setScopeId(scopeId);
                req.setMemoryType(memoryType);
                req.setPageSize(pageSize);
                req.setPageIdx(pageIdx);
                PageResult<MemoryItem> page = client.getUserMemByPage(req);
                if (page == null || page.items() == null || page.items().isEmpty()) {
                    break;
                }
                count += page.items().size();
                if (page.items().size() < pageSize) {
                    break;
                }
                pageIdx++;
            }
            return count;
        } catch (Exception e) {
            log.warn("countMemoriesByType failed: type={}, user={}, scope={}, err={}", memoryType, userId, scopeId, e.getMessage());
            return 0;
        }
    }

    /**
     * 构建最近7天的增长趋势。
     * 从 memory_change_log_snapshot 按 change_type 聚合每日写入/修改/删除数。
     */
    private List<MemoryStatsDTO.GrowthTrend> buildGrowthTrend() {
        Instant startTime = Instant.now().minus(java.time.Duration.ofDays(GROWTH_TREND_DAYS));
        List<MemoryChangeLogSnapshotEntity> snapshots = changeLogMapper.findByCreatedAtAfter(startTime);

        // 按日期分组
        Map<String, long[]> dailyMap = new LinkedHashMap<>();
        LocalDate today = LocalDate.now();
        for (int i = GROWTH_TREND_DAYS - 1; i >= 0; i--) {
            LocalDate date = today.minusDays(i);
            dailyMap.put(date.toString(), new long[]{0, 0, 0}); // created, updated, deleted
        }

        for (MemoryChangeLogSnapshotEntity snap : snapshots) {
            if (snap.getCreatedAt() == null) continue;
            String dateKey = snap.getCreatedAt()
                    .atZone(ZoneId.systemDefault())
                    .toLocalDate()
                    .toString();
            long[] counts = dailyMap.get(dateKey);
            if (counts == null) continue;
            switch (snap.getChangeType()) {
                case "CREATE" -> counts[0]++;
                case "UPDATE" -> counts[1]++;
                case "DELETE" -> counts[2]++;
                default -> {}
            }
        }

        List<MemoryStatsDTO.GrowthTrend> result = new ArrayList<>();
        for (Map.Entry<String, long[]> entry : dailyMap.entrySet()) {
            long[] counts = entry.getValue();
            result.add(MemoryStatsDTO.GrowthTrend.builder()
                    .date(entry.getKey())
                    .created(counts[0])
                    .updated(counts[1])
                    .deleted(counts[2])
                    .netGrowth(counts[0] - counts[2])
                    .build());
        }
        return result;
    }

    /**
     * 构建按用户统计 Top10。
     * V3 §6.6 整改：经 MessageLogService 代理内核 KR-MSG-02（不再读 request_response_logs 死表）。
     * 内核无 by-user 聚合端点，MessageLogService.statsByUser 以总量近似返回，此处容错截取。
     */
    private List<MemoryStatsDTO.UserStats> buildTopUsers(String adminUserId) {
        try {
            Instant startTime = Instant.now().minus(java.time.Duration.ofDays(7));
            List<Map<String, Object>> rawStats = messageLogService.statsByUser(adminUserId, startTime, null);
            List<MemoryStatsDTO.UserStats> result = new ArrayList<>();
            int limit = 0;
            for (Map<String, Object> row : rawStats) {
                if (limit >= TOP_USERS_LIMIT) break;
                // 兼容两种 key：V2 的 itemType 与新代理的 user_id
                Object uidObj = row.get("user_id") != null ? row.get("user_id") : row.get("itemType");
                String uid = uidObj == null ? "unknown" : String.valueOf(uidObj);
                long count = 0;
                Object cntObj = row.get("count");
                if (cntObj instanceof Number n) {
                    count = n.longValue();
                }
                result.add(MemoryStatsDTO.UserStats.builder()
                        .userId(uid)
                        .messageCount(count)
                        .build());
                limit++;
            }
            return result;
        } catch (Exception e) {
            log.warn("buildTopUsers failed: adminUserId={}, err={}", adminUserId, e.getMessage());
            return Collections.emptyList();
        }
    }

    /**
     * 构建存储用量。
     * 从 tenant_quotas 读取配额上限和当前用量。
     */
    private MemoryStatsDTO.StorageUsage buildStorageUsage(String adminUserId) {
        try {
            TenantQuotaEntity quota = quotaMapper.findByAdminUserId(adminUserId);
            if (quota == null) {
                return MemoryStatsDTO.StorageUsage.builder()
                        .usedMb(0.0)
                        .maxMb(10240.0)
                        .percentage(0.0)
                        .build();
            }
            double used = quota.getCurrentStorageMb() == null ? 0.0 : quota.getCurrentStorageMb();
            double max = quota.getMaxStorageMb() == null ? 10240.0 : quota.getMaxStorageMb();
            double pct = max > 0 ? Math.round(used * 1000.0 / max) / 10.0 : 0.0;
            return MemoryStatsDTO.StorageUsage.builder()
                    .usedMb(used)
                    .maxMb(max)
                    .percentage(pct)
                    .build();
        } catch (Exception e) {
            log.warn("buildStorageUsage failed: adminUserId={}, err={}", adminUserId, e.getMessage());
            return MemoryStatsDTO.StorageUsage.builder()
                    .usedMb(0.0)
                    .maxMb(10240.0)
                    .percentage(0.0)
                    .build();
        }
    }
}
