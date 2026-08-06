package com.openjiuwen.memory.logcenter.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Builder;
import lombok.Data;

import java.util.List;
import java.util.Map;

/**
 * 日志统计仪表盘数据。
 * 根据 logType 返回不同维度的统计信息。
 */
@Data
@Builder
public class LogStatsDTO {

    /** 日志类型: operations/runtime/messages */
    @JsonProperty("log_type")
    private String logType;

    /** 总数 */
    private long total;

    /** 按维度统计 (itemType -> count) */
    @JsonProperty("by_dimension")
    private List<Map<String, Object>> byDimension;

    /** 错误率(操作日志) 或 错误数(运行日志) */
    @JsonProperty("error_rate")
    private Double errorRate;

    /** 平均响应时间(消息日志) */
    @JsonProperty("avg_response_time_ms")
    private Double avgResponseTimeMs;

    /** 生成记忆数(消息日志) */
    @JsonProperty("memory_generated_count")
    private Long memoryGeneratedCount;
}
