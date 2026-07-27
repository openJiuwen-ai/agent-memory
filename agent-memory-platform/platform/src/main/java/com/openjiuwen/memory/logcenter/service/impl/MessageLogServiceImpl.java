package com.openjiuwen.memory.logcenter.service.impl;

import com.baomidou.mybatisplus.core.metadata.IPage;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.logcenter.domain.MessageLogEntity;
import com.openjiuwen.memory.logcenter.service.MessageLogService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 用户消息日志服务实现（V3 §6.6 整改后）。
 * <p>
 * 整改前（V2 死代码）：读服务层 request_response_logs 表 —— 该表由 MessageLogFilter 拦截
 * /api/v1/memories/** 写入，但 V3 架构下 Agent 直连内核，服务层无此路径，表恒为空。
 * <p>
 * 整改后（V3 §6.6）：消息不经过服务层，数据源 = 内核 user_message 表，
 * 通过内核 KR-MSG-01~04（/admin/messages/*）非拦截式查询，接口签名保持不变，
 * 下游（MessageLogController / UiAggregatorServiceImpl / LogStatsServiceImpl /
 * LogCollectAsyncService）零改动。
 * <p>
 * 字段映射（内核 item → MessageLogEntity）：
 *   message_id→id/requestId, user_id→userId, scope_id→scopeName,
 *   role→messageRoles, timestamp→createdAt, apiPath 常量化 "/add_messages/"。
 * V2 专有的请求级字段（responseStatus/responseTimeMs/memoryGenerated/clientIp 等）
 * 在内核消息模型中不存在，一律留空。
 */
@Service
public class MessageLogServiceImpl implements MessageLogService {

    private static final Logger log = LoggerFactory.getLogger(MessageLogServiceImpl.class);

    /** 内核中间记忆的 scope_id 标记值，此类记录为中间产物，非真实用户消息，消息日志中默认过滤。 */
    private static final String MIDDLE_TERM_MEMORY_SCOPE = "middle_term_memory";

    private final MemoryEngineClient memoryEngineClient;

    public MessageLogServiceImpl(MemoryEngineClient memoryEngineClient) {
        this.memoryEngineClient = memoryEngineClient;
    }

    @Override
    public IPage<MessageLogEntity> queryLogs(String adminUserId, String userId, String scopeName,
                                             Boolean successOnly, Instant startTime, Instant endTime,
                                             int page, int size) {
        Map<String, Object> filter = new LinkedHashMap<>();
        if (userId != null && !userId.isBlank()) filter.put("user_id", userId);
        if (scopeName != null && !scopeName.isBlank()) filter.put("scope_id", scopeName);
        if (startTime != null) filter.put("start_time", startTime.toString());
        if (endTime != null) filter.put("end_time", endTime.toString());
        filter.put("page_idx", Math.max(1, page));
        filter.put("page_size", Math.max(1, Math.min(size, 200)));
        // successOnly 是 V2 请求日志概念（HTTP 状态），内核消息无此维度，忽略。
        // 内核侧过滤中间记忆产物（scope_id=middle_term_memory），非真实用户消息。
        // 仅当未显式查询 middle_term_memory 时才排除（用户想看中间产物时不排除）。
        if (!MIDDLE_TERM_MEMORY_SCOPE.equals(scopeName)) {
            filter.put("exclude_scopes", List.of(MIDDLE_TERM_MEMORY_SCOPE));
        }

        Map<String, Object> body = memoryEngineClient.queryKernelMessages(filter);
        long total = asLong(body.get("total"));
        List<MessageLogEntity> records = new ArrayList<>();
        Object items = body.get("items");
        if (items instanceof List<?> list) {
            for (Object o : list) {
                if (o instanceof Map<?, ?> m) {
                    records.add(toEntity(m));
                }
            }
        }
        // 内核已在 SQL 层排除中间产物，total 和 items 均不含 middle_term_memory，分页正确。
        Page<MessageLogEntity> result = new Page<>(Math.max(1, page), Math.max(1, size));
        result.setTotal(total);
        result.setRecords(records);
        return result;
    }

    @Override
    public List<Map<String, Object>> statsByUser(String adminUserId, Instant startTime, Instant endTime) {
        // 内核 KR-MSG-02 按 role 聚合，无 by-user 维度；这里以"当前过滤条件下总量"近似，
        // 保持返回形状 [{user_id, count}]，避免下游 NPE。需要精确 by-user 请走 KR-MSG-01 自行聚合。
        Map<String, Object> body = memoryEngineClient.statsKernelMessages(
                null, null, null,
                startTime == null ? null : startTime.toString(),
                endTime == null ? null : endTime.toString());
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("user_id", "__all__");
        row.put("count", asLong(body.get("total")));
        return List.of(row);
    }

    @Override
    public List<Map<String, Object>> statsByScope(String adminUserId, Instant startTime, Instant endTime) {
        // 同上：内核无 by-scope 聚合端点，以总量近似保持形状 [{scope_name, count}]。
        Map<String, Object> body = memoryEngineClient.statsKernelMessages(
                null, null, null,
                startTime == null ? null : startTime.toString(),
                endTime == null ? null : endTime.toString());
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("scope_name", "__all__");
        row.put("count", asLong(body.get("total")));
        return List.of(row);
    }

    @Override
    public long count(String adminUserId, Instant startTime, Instant endTime) {
        Map<String, Object> body = memoryEngineClient.statsKernelMessages(
                null, null, null,
                startTime == null ? null : startTime.toString(),
                endTime == null ? null : endTime.toString());
        return asLong(body.get("total"));
    }

    @Override
    public long countMemoryGenerated(String adminUserId, Instant startTime, Instant endTime) {
        // V2 请求日志字段 memory_generated 在内核消息模型中不存在；消息入库即视为已生成记忆输入，
        // 以总量近似（与 count 一致），保持 LogStatsServiceImpl 不 NPE。
        return count(adminUserId, startTime, endTime);
    }

    @Override
    public double avgResponseTime(String adminUserId, Instant startTime, Instant endTime) {
        // 内核消息模型无响应时间维度（V3 §6.6 消息不经过服务层，无请求耗时概念），返回 0。
        return 0.0d;
    }

    @Override
    public String exportToCsv(String adminUserId, String userId, String scopeName,
                              Boolean successOnly, Instant startTime, Instant endTime) {
        byte[] bytes = memoryEngineClient.exportKernelMessages(
                scopeName, userId, null,
                startTime == null ? null : startTime.toString(),
                endTime == null ? null : endTime.toString(),
                20000);
        String csv = new String(bytes, java.nio.charset.StandardCharsets.UTF_8);
        // 保持 V2 行为：带 BOM，便于 Excel 打开。
        return csv.startsWith("\uFEFF") ? csv : "\uFEFF" + csv;
    }

    // —— 内部工具 ——

    /** 内核 item(Map) → MessageLogEntity（V3 字段映射，V2 专有字段留空）。 */
    private MessageLogEntity toEntity(Map<?, ?> m) {
        MessageLogEntity e = new MessageLogEntity();
        String messageId = asString(m.get("message_id"));
        e.setId(messageId);
        e.setRequestId(messageId);
        e.setUserId(asString(m.get("user_id")));
        e.setScopeName(asString(m.get("scope_id")));
        e.setApiPath("/add_messages/");
        e.setApiMethod("POST");
        e.setMessageCount(1);
        e.setMessageRoles(asString(m.get("role")));
        String ts = asString(m.get("timestamp"));
        if (ts != null && !ts.isBlank()) {
            try {
                e.setCreatedAt(Instant.parse(ts));
            } catch (Exception ex) {
                log.debug("无法解析内核消息时间戳: {}", ts);
            }
        }
        return e;
    }

    private static String asString(Object o) {
        return o == null ? null : String.valueOf(o);
    }

    private static long asLong(Object o) {
        if (o instanceof Number n) return n.longValue();
        if (o == null) return 0L;
        try {
            return Long.parseLong(String.valueOf(o));
        } catch (NumberFormatException e) {
            return 0L;
        }
    }
}
