package com.openjiuwen.memory.logcenter.controller;

import com.baomidou.mybatisplus.core.metadata.IPage;
import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.logcenter.domain.OperationLogEntity;
import com.openjiuwen.memory.logcenter.service.OperationLogService;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * 操作审计日志查询控制器（§6.4.1）。
 * <p>
 * 提供操作审计日志的分页查询、统计和 CSV 导出接口。
 * 日志写入由 AuditLogFilter 自动拦截记录。
 */
@RestController
@RequestMapping("/api/v1/logs/operations")
public class OperationLogController {

    private final OperationLogService operationLogService;
    private final PermissionChecker permissionChecker;

    public OperationLogController(OperationLogService operationLogService,
                                  PermissionChecker permissionChecker) {
        this.operationLogService = operationLogService;
        this.permissionChecker = permissionChecker;
    }

    /**
     * 分页查询操作审计日志。
     */
    @GetMapping
    public ApiResponse<IPage<OperationLogEntity>> queryLogs(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "operator_id", required = false) String operatorId,
            @RequestParam(name = "type", required = false) String operationType,
            @RequestParam(name = "success_only", required = false) Boolean successOnly,
            @RequestParam(name = "start", required = false) String startTime,
            @RequestParam(name = "end", required = false) String endTime,
            @RequestParam(name = "page", defaultValue = "1") int page,
            @RequestParam(name = "size", defaultValue = "20") int size) {
        permissionChecker.require("log:read");
        // 分页参数校验（BUG-LOG-13/14 修复：无效分页参数应返回 400）
        if (page < 0) {
            throw new BizException(ResultCode.BAD_REQUEST, "page 参数不能为负数");
        }
        if (size < 1) {
            throw new BizException(ResultCode.BAD_REQUEST, "size 参数必须大于 0");
        }
        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);
        IPage<OperationLogEntity> result = operationLogService.queryLogs(
                adminUserId, operatorId, operationType, successOnly, start, end, page, size);
        return ApiResponse.ok(result);
    }

    /**
     * 按操作类型统计。
     */
    @GetMapping("/stats/by-type")
    public ApiResponse<List<Map<String, Object>>> statsByType(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "start", required = false) String startTime,
            @RequestParam(name = "end", required = false) String endTime) {
        permissionChecker.require("log:read");
        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);
        return ApiResponse.ok(operationLogService.statsByType(adminUserId, start, end));
    }

    /**
     * 按操作人统计。
     */
    @GetMapping("/stats/by-operator")
    public ApiResponse<List<Map<String, Object>>> statsByOperator(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "start", required = false) String startTime,
            @RequestParam(name = "end", required = false) String endTime) {
        permissionChecker.require("log:read");
        Instant start = parseInstant(startTime);
        Instant end = parseInstant(endTime);
        return ApiResponse.ok(operationLogService.statsByOperator(adminUserId, start, end));
    }

    /**
     * 导出操作审计日志为 CSV（§6.4.1）。
     * 返回带 UTF-8 BOM 的 CSV 文件，确保 Excel 正确识别中文。
     * 时间范围限制：start/end 必填，最大 7 天（P0-3 整改，防止 StringBuilder 无界增长 OOM）。
     */
    @GetMapping("/export")
    public ResponseEntity<ByteArrayResource> exportToCsv(
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "operator_id", required = false) String operatorId,
            @RequestParam(name = "type", required = false) String operationType,
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
        String csv = operationLogService.exportToCsv(
                adminUserId, operatorId, operationType, successOnly, start, end);
        byte[] bytes = csv.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        String filename = "operation-logs-" + java.time.LocalDate.now() + ".csv";
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + filename + "\"")
                .contentType(MediaType.parseMediaType("text/csv; charset=UTF-8"))
                .contentLength(bytes.length)
                .body(new ByteArrayResource(bytes));
    }

    private Instant parseInstant(String iso) {
        if (iso == null || iso.isBlank()) {
            return null;
        }
        try {
            return Instant.parse(iso);
        } catch (Exception e) {
            return null;
        }
    }
}
