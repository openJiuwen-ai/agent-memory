package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.opscenter.dto.MemoryStatsDTO;

/**
 * 记忆统计仪表盘服务（§7.5.2）。
 * <p>
 * 服务层增量价值：
 * <ul>
 *   <li>内核 user_mem_total_num 只返回单用户单 scope 总数，服务层跨 scope 聚合</li>
 *   <li>从 memory_change_log_snapshot 聚合增长趋势（纯服务层数据）</li>
 *   <li>从 request_response_logs 聚合按用户统计 Top10（纯服务层数据）</li>
 *   <li>从 tenant_quotas 获取存储用量</li>
 * </ul>
 */
public interface MemoryStatsService {

    /**
     * 获取记忆统计仪表盘数据。
     *
     * @param adminUserId 管理员/租户 ID
     * @param scopeId     指定 scope（可为 null，表示跨 scope 聚合）
     * @param userId      指定用户（可为 null）
     * @return 统计仪表盘数据
     */
    MemoryStatsDTO getMemoryStats(String adminUserId, String scopeId, String userId);
}
