package com.openjiuwen.memory.logcenter.task;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.openjiuwen.memory.logcenter.domain.OperationLogEntity;
import com.openjiuwen.memory.logcenter.mapper.OperationLogMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.time.temporal.ChronoUnit;

/**
 * 日志保留期清理任务（V3 整改后）。
 * <p>
 * 每天 03:00 执行，删除 operation_logs 中超过保留期的记录。
 * <p>
 * V3 整改（2026-07-21，§6.6）：摘除 request_response_logs 清理分支 ——
 * 该表属 V2 死代码（MessageLogFilter 已停用，无写入方），消息日志数据源
 * 已切换为内核 user_message 表（由内核自行管理生命周期，服务层不清理）。
 */
@Component
public class LogRetentionCleanupTask {

    private static final Logger log = LoggerFactory.getLogger(LogRetentionCleanupTask.class);

    /** 日志保留天数（设计文档 §6.3.4） */
    private static final int RETENTION_DAYS = 7;

    private final OperationLogMapper operationLogMapper;

    public LogRetentionCleanupTask(OperationLogMapper operationLogMapper) {
        this.operationLogMapper = operationLogMapper;
    }

    /**
     * 每天 03:00 清理过期日志。
     * cron 格式：秒 分 时 日 月 周
     */
    @Scheduled(cron = "0 0 3 * * ?")
    public void cleanupExpiredLogs() {
        Instant cutoff = Instant.now().minus(RETENTION_DAYS, ChronoUnit.DAYS);
        log.info("开始清理 {} 天前（{} 之前）的过期日志", RETENTION_DAYS, cutoff);

        // 仅清理操作日志；request_response_logs 为 V2 死表（无写入方），不再清理。
        try {
            LambdaQueryWrapper<OperationLogEntity> opWrapper = new LambdaQueryWrapper<>();
            opWrapper.lt(OperationLogEntity::getOperatedAt, cutoff);
            int opDeleted = operationLogMapper.delete(opWrapper);
            if (opDeleted > 0) {
                log.info("清理过期操作日志 {} 条", opDeleted);
            }
        } catch (Exception e) {
            log.error("清理过期操作日志失败: {}", e.getMessage(), e);
        }
    }
}
