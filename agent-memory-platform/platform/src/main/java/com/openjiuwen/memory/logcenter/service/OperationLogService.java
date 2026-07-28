package com.openjiuwen.memory.logcenter.service;

import com.baomidou.mybatisplus.core.metadata.IPage;
import com.openjiuwen.memory.logcenter.domain.OperationLogEntity;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * 操作审计日志服务 — 查询操作审计日志、统计仪表盘数据。
 * 日志写入由 AuditLogFilter 自动拦截记录，本服务提供查询和统计能力。
 */
public interface OperationLogService {

    /**
     * 分页查询操作审计日志。
     *
     * @param adminUserId   管理员ID（租户隔离）
     * @param operatorId    操作人ID（可空）
     * @param operationType 操作类型（可空）
     * @param successOnly   仅成功操作（可空）
     * @param startTime     开始时间（可空）
     * @param endTime       结束时间（可空）
     * @param page          页码
     * @param size          每页大小
     */
    IPage<OperationLogEntity> queryLogs(String adminUserId, String operatorId,
                                        String operationType, Boolean successOnly,
                                        Instant startTime, Instant endTime,
                                        int page, int size);

    /**
     * 按操作类型统计。
     */
    List<Map<String, Object>> statsByType(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 按操作人统计。
     */
    List<Map<String, Object>> statsByOperator(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 统计总操作数。
     */
    long count(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 统计错误率。
     */
    double errorRate(String adminUserId, Instant startTime, Instant endTime);

    /**
     * 导出操作审计日志为 CSV（§6.4.1）。
     * 按过滤条件查询全量记录（不分页），转成 CSV 字符串（UTF-8 BOM）。
     *
     * @param adminUserId   管理员ID（租户隔离）
     * @param operatorId    操作人ID（可空）
     * @param operationType 操作类型（可空）
     * @param successOnly   仅成功操作（可空）
     * @param startTime     开始时间（可空）
     * @param endTime       结束时间（可空）
     * @return CSV 字符串
     */
    String exportToCsv(String adminUserId, String operatorId,
                        String operationType, Boolean successOnly,
                        Instant startTime, Instant endTime);
}
