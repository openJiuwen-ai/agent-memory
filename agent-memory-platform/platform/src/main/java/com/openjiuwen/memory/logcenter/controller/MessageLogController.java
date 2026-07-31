package com.openjiuwen.memory.logcenter.controller;

import com.baomidou.mybatisplus.core.metadata.IPage;
import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.logcenter.domain.MessageLogEntity;
import com.openjiuwen.memory.logcenter.dto.MemoryWithMetadataDTO;
import com.openjiuwen.memory.logcenter.service.MessageLogService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 用户消息日志查询控制器（V3 §6.6 整改后）。
 * <p>
 * 消息日志分两级：
 * <ul>
 *   <li>L1 文件级：通过内核 /logs/tail?event_type=message 获取内核原始消息日志文件，不入库。
 *       L1 文件下载统一由 RuntimeLogController /download?filename= 管理（先查询后下载模式）。</li>
 *   <li>L2 消息记录级：数据源 = 内核 user_message 表，由 MessageLogService 代理内核
 *       KR-MSG-01~04（/admin/messages/*）查询，提供分页查询、统计、CSV 导出。
 *       V3 整改：不再经 MessageLogFilter 落库 request_response_logs（V2 死代码，已停用）。</li>
 * </ul>
 * 接口路径与参数形状保持不变（/api/v1/logs/messages/** + log:read 权限），前端零改动。
 */
@RestController
@RequestMapping("/api/v1/logs/messages")
public class MessageLogController {

    private static final Logger log = LoggerFactory.getLogger(MessageLogController.class);

    private final MessageLogService messageLogService;
    private final MemoryEngineClient memoryEngineClient;
    private final PermissionChecker permissionChecker;

    public MessageLogController(MessageLogService messageLogService,
                               MemoryEngineClient memoryEngineClient,
                               PermissionChecker permissionChecker) {
        this.messageLogService = messageLogService;
        this.memoryEngineClient = memoryEngineClient;
        this.permissionChecker = permissionChecker;
    }

    // ==================== L2: DB 分页查询 + 统计 ====================

    /**
     * 分页查询用户消息日志（L2 DB）。
     */
    @GetMapping
    public ApiResponse<IPage<MessageLogEntity>> queryLogs(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "user_id", required = false) String userId,
            @RequestParam(name = "scope_name", required = false) String scopeName,
            @RequestParam(name = "success_only", required = false) Boolean successOnly,
            @RequestParam(name = "start", required = false) String startTime,
            @RequestParam(name = "end", required = false) String endTime,
            @RequestParam(name = "page", defaultValue = "1") int page,
            @RequestParam(name = "size", defaultValue = "20") int size) {
        permissionChecker.require("log:read");
        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);
        IPage<MessageLogEntity> result = messageLogService.queryLogs(
                adminUserId, userId, scopeName, successOnly, start, end, page, size);
        return ApiResponse.ok(result);
    }

    /**
     * 分页查询用户消息日志（POST 方式，V3 §4.6 API#6 兼容）。
     * 与 GET 端点功能一致，支持 POST 请求体传参。
     */
    @PostMapping
    public ApiResponse<IPage<MessageLogEntity>> queryLogsPost(@RequestBody Map<String, Object> body) {
        permissionChecker.require("log:read");
        String adminUserId = body.get("admin_user_id") != null ? String.valueOf(body.get("admin_user_id")) : null;
        String userId = body.get("user_id") != null ? String.valueOf(body.get("user_id")) : null;
        String scopeName = body.get("scope_name") != null ? String.valueOf(body.get("scope_name")) : null;
        Boolean successOnly = body.get("success_only") instanceof Boolean b ? b : null;
        String startTime = body.get("start") != null ? String.valueOf(body.get("start")) : null;
        String endTime = body.get("end") != null ? String.valueOf(body.get("end")) : null;
        int page = body.get("page") instanceof Number n ? n.intValue() : 1;
        int size = body.get("size") instanceof Number n2 ? n2.intValue() : 20;
        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);
        IPage<MessageLogEntity> result = messageLogService.queryLogs(
                adminUserId, userId, scopeName, successOnly, start, end, page, size);
        return ApiResponse.ok(result);
    }

    /**
     * 按用户统计消息数量。
     */
    @GetMapping("/stats/by-user")
    public ApiResponse<List<Map<String, Object>>> statsByUser(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "start", required = false) String startTime,
            @RequestParam(name = "end", required = false) String endTime) {
        permissionChecker.require("log:read");
        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);
        return ApiResponse.ok(messageLogService.statsByUser(adminUserId, start, end));
    }

    /**
     * 按Scope统计消息数量。
     */
    @GetMapping("/stats/by-scope")
    public ApiResponse<List<Map<String, Object>>> statsByScope(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "start", required = false) String startTime,
            @RequestParam(name = "end", required = false) String endTime) {
        permissionChecker.require("log:read");
        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);
        return ApiResponse.ok(messageLogService.statsByScope(adminUserId, start, end));
    }

    /**
     * 导出消息日志为 CSV（L2 DB，§6.4.3）。
     * 时间范围限制：start/end 必填，最大 7 天（P0-3 整改，防止 StringBuilder 无界增长 OOM）。
     */
    @GetMapping("/export")
    public ResponseEntity<ByteArrayResource> exportToCsv(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "user_id", required = false) String userId,
            @RequestParam(name = "scope_name", required = false) String scopeName,
            @RequestParam(name = "success_only", required = false) Boolean successOnly,
            @RequestParam(name = "start") String startTime,
            @RequestParam(name = "end") String endTime) {
        permissionChecker.require("log:read");
        if (startTime == null || startTime.isBlank() || endTime == null || endTime.isBlank()) {
            throw new BizException(ResultCode.BAD_REQUEST, "导出必须指定 start 和 end 时间范围（最大7天）");
        }
        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);
        if (start == null || end == null) {
            throw new BizException(ResultCode.BAD_REQUEST, "start/end 时间格式无效，请使用 ISO-8601 格式");
        }
        if (start.isAfter(end)) {
            throw new BizException(ResultCode.BAD_REQUEST, "开始时间不能晚于结束时间");
        }
        if (Duration.between(start, end).toDays() > 7) {
            throw new BizException(ResultCode.BAD_REQUEST, "导出范围不能超过7天");
        }
        String csv = messageLogService.exportToCsv(
                adminUserId, userId, scopeName, successOnly, start, end);
        byte[] bytes = csv.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        String filename = "message-logs-" + java.time.LocalDate.now() + ".csv";
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + filename + "\"")
                .contentType(MediaType.parseMediaType("text/csv; charset=UTF-8"))
                .contentLength(bytes.length)
                .body(new ByteArrayResource(bytes));
    }

    /**
     * 查询单条用户消息详情（V3 §4.6 API#9，代理内核 KR-MSG-04）。
     */
    @GetMapping("/{msgId}")
    public ApiResponse<Map<String, Object>> getMessageDetail(@PathVariable String msgId) {
        permissionChecker.require("log:read");
        
        // 校验 msgId 有效性
        if (msgId == null || msgId.isBlank()) {
            throw new BizException(ResultCode.BAD_REQUEST, "消息 ID 不能为空");
        }
        
        Map<String, Object> detail = memoryEngineClient.getKernelMessageDetail(msgId);
        
        // V3-DEFECT-047: 区分"资源不存在"和"系统错误"
        if (detail == null) {
            // 消息不存在 → 返回 404
            throw new BizException(ResultCode.NOT_FOUND, "消息不存在：" + msgId);
        }
        
        Boolean found = (Boolean) detail.get("found");
        if (Boolean.FALSE.equals(found)) {
            // 明确告知消息不存在
            throw new BizException(ResultCode.NOT_FOUND, "消息不存在：" + msgId);
        }
        
        return ApiResponse.ok(detail);
    }
    
    /**
     * V3-DEFECT-058: 获取记忆完整元数据
     */
    @GetMapping("/metadata/{memId}")
    public ApiResponse<MemoryWithMetadataDTO> getWithMetadata(
            @PathVariable String memId,
            @RequestParam(name = "user_id", required = false) String userId,
            @RequestParam(name = "scope_id", required = false) String scopeId,
            @RequestParam(name = "session_id", required = false) String sessionId) {
        
        permissionChecker.require("log:read");
        
        // 通过内核 API 获取元数据
        Map<String, Object> metadataMap = memoryEngineClient.getMemoryWithMetadata(userId, scopeId, memId);
        
        if (metadataMap == null || metadataMap.isEmpty()) {
            return ApiResponse.fail(50000, "无法获取记忆元数据");
        }
        
        MemoryWithMetadataDTO metadata = MemoryWithMetadataDTO.builder()
                .messageId(String.valueOf(metadataMap.getOrDefault("message_id", memId)))
                .userId(String.valueOf(metadataMap.getOrDefault("user_id", userId != null ? userId : "__default__")))
                .scopeId(String.valueOf(metadataMap.getOrDefault("scope_id", scopeId != null ? scopeId : "__default__")))
                .sessionId(String.valueOf(metadataMap.getOrDefault("session_id", "")))
                .role(String.valueOf(metadataMap.getOrDefault("role", "")))
                .content(String.valueOf(metadataMap.getOrDefault("content", "")))
                .timestamp(String.valueOf(metadataMap.getOrDefault("timestamp", "")))
                .build();
        
        return ApiResponse.ok(metadata);
    }

    // ==================== L1: 文件 tail（调内核 HTTP） ====================

    /**
     * 瞬时查询内核消息日志文件（L1，调内核 /logs/tail?event_type=message）。
     * 不入库，直接转发内核返回的最近 N 行。
     *
     * @param lines 读取行数（默认500，最大2000）
     * @param level 日志级别过滤（可空）
     */
    @GetMapping("/tail")
    public ApiResponse<Map<String, Object>> tailMessageLogs(
            @RequestParam(name = "lines", defaultValue = "500") int lines,
            @RequestParam(name = "level", required = false) String level) {
        permissionChecker.require("log:read");
        try {
            List<String> linesList = memoryEngineClient.tailKernelLogs(lines, level, "message");
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("lines", linesList);
            result.put("total", linesList.size());
            return ApiResponse.ok(result);
        } catch (Exception e) {
            log.warn("调用内核 /logs/tail?type=message 失败: {}", e.getMessage());
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("lines", List.of());
            result.put("total", 0);
            result.put("error", e.getMessage());
            return ApiResponse.ok(result);
        }
    }

    private Instant parseInstant(String iso) {
        if (iso == null || iso.isBlank()) {
            return null;
        }
        try {
            return Instant.parse(iso);
        } catch (Exception e) {
            // V3-DEFECT-046: 严格验证时间格式，不再静默忽略
            throw new BizException(ResultCode.BAD_REQUEST, 
                String.format("时间格式无效，请使用 ISO 8601 格式 (如 2026-01-01T00:00:00Z): %s", iso));
        }
    }
}
