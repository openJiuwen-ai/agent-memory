package com.openjiuwen.memory.logcenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.common.util.PathValidator;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDate;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 运行日志查询控制器 — 2026-07-21 v5 重构
 * <p>
 * 对齐 §6.3.2 + §6.4.2：运行日志不入库，服务层通过 HTTP 调用内核接口获取日志。
 * 运行日志不提供日志级别动态管理功能（§6.3.2 设计说明）。
 * <ul>
 *   <li>GET /api/v1/logs/runtime/tail      瞬时查询（调内核 /logs/tail，默认500条）</li>
 *   <li>GET /api/v1/logs/runtime/files     列出可下载的内核日志文件（调内核 /logs/files）</li>
 *   <li>GET /api/v1/logs/runtime/download  按文件名下载（调内核 /logs/download?filename=...）</li>
 * </ul>
 * 旧的 DB 分页查询 /stats/by-* /db/export /level 端点已删除（运行日志不入库 + 不提供级别管理）。
 */
@RestController
@RequestMapping("/api/v1/logs/runtime")
public class RuntimeLogController {

    private static final Logger log = LoggerFactory.getLogger(RuntimeLogController.class);

    private final MemoryEngineClient memoryEngineClient;
    private final PermissionChecker permissionChecker;
    private final com.openjiuwen.memory.logcenter.service.PlatformLogReader platformLogReader;

    public RuntimeLogController(MemoryEngineClient memoryEngineClient,
                                  PermissionChecker permissionChecker,
                                  com.openjiuwen.memory.logcenter.service.PlatformLogReader platformLogReader) {
        this.memoryEngineClient = memoryEngineClient;
        this.permissionChecker = permissionChecker;
        this.platformLogReader = platformLogReader;
    }

    /**
     * 瞬时查询：获取最近 N 行运行日志。
     * source=kernel（默认）调内核 /logs/tail；source=platform 读服务层自身 platform.log。
     *
     * @param lines     读取行数（默认500，最大2000）
     * @param level     日志级别过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL，可空）
     * @param eventType 事件类型过滤（可空）
     * @param source    日志来源：kernel（默认）/ platform
     */
    @GetMapping("/tail")
    public ApiResponse<Map<String, Object>> tailLogs(
            @RequestParam(name = "lines", defaultValue = "500") int lines,
            @RequestParam(name = "level", required = false) String level,
            @RequestParam(name = "event_type", required = false) String eventType,
            @RequestParam(name = "source", defaultValue = "kernel") String source) {
        permissionChecker.require("log:read");
        if ("platform".equalsIgnoreCase(source)) {
            return ApiResponse.ok(platformLogReader.tail(lines, level, eventType));
        }
        try {
            List<String> linesList = memoryEngineClient.tailKernelLogs(lines, level, eventType);
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("lines", linesList);
            result.put("total", linesList.size());
            return ApiResponse.ok(result);
        } catch (Exception e) {
            log.warn("调用内核 /logs/tail 失败，返回空列表: {}", e.getMessage());
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("lines", List.of());
            result.put("total", 0);
            result.put("error", e.getMessage());
            return ApiResponse.ok(result);
        }
    }

    /**
     * 列出可下载的内核运行日志文件项（下载前先看有哪些）。
     * 调用内核 GET /logs/files 获取真实文件列表（先查询后下载模式）。
     * 内核返回每个文件的 filename/log_type/size_bytes/size_human/modified_at/is_rotated。
     *
     * @param startDate 开始日期（可选，按文件修改时间过滤）
     * @param endDate   结束日期（可选，按文件修改时间过滤）
     */
    @GetMapping("/files")
    public ApiResponse<List<Map<String, Object>>> listLogFiles(
            @RequestParam(name = "start_date", required = false) String startDateStr,
            @RequestParam(name = "end_date", required = false) String endDateStr,
            @RequestParam(name = "source", defaultValue = "kernel") String source) {
        permissionChecker.require("log:read");
        // 解析日期范围（platform / kernel 共用的过滤参数）
        LocalDate startDate = (startDateStr == null || startDateStr.isBlank())
                ? null : LocalDate.parse(startDateStr);
        LocalDate endDate = (endDateStr == null || endDateStr.isBlank())
                ? null : LocalDate.parse(endDateStr);
        if ("platform".equalsIgnoreCase(source)) {
            return ApiResponse.ok(platformLogReader.listFiles(startDate, endDate));
        }
        try {
            List<Map<String, Object>> kernelFiles = memoryEngineClient.listKernelLogFiles();
            // 按日期范围过滤（可选，不传则返回全部）
            if (startDateStr != null && !startDateStr.isBlank()
                    || endDateStr != null && !endDateStr.isBlank()) {
                // 与上方 platform 分支共用 startDate/endDate：缺省一侧补默认值
                if (startDate == null) {
                    startDate = LocalDate.now().minusDays(6);
                }
                if (endDate == null) {
                    endDate = LocalDate.now();
                }
                if (startDate.isAfter(endDate)) {
                    throw new BizException(ResultCode.BAD_REQUEST, "开始日期不能晚于结束日期");
                }
                long days = ChronoUnit.DAYS.between(startDate, endDate);
                if (days > 7) {
                    throw new BizException(ResultCode.BAD_REQUEST, "范围不能超过7天");
                }
                final LocalDate fStart = startDate;
                final LocalDate fEnd = endDate;
                kernelFiles = kernelFiles.stream()
                        .filter(item -> {
                            String modifiedAt = (String) item.get("modified_at");
                            if (modifiedAt == null) return true;
                            try {
                                LocalDate fileDate = LocalDate.parse(modifiedAt.substring(0, 10));
                                return !fileDate.isBefore(fStart) && !fileDate.isAfter(fEnd);
                            } catch (Exception e) {
                                return true;
                            }
                        })
                        .collect(java.util.stream.Collectors.toList());
            }
            return ApiResponse.ok(kernelFiles);
        } catch (BizException e) {
            throw e;
        } catch (Exception e) {
            log.warn("调用内核 /logs/files 失败，返回空列表: {}", e.getMessage());
            return ApiResponse.ok(List.of());
        }
    }

    /**
     * 按文件名下载：调用内核 /logs/download?filename=... 获取指定日志文件。
     * 先查询后下载模式：前端先调 /files 获取文件列表，用户选择具体文件后调此接口下载。
     *
     * @param filename 日志文件相对路径（由 /files 返回的 filename 字段，如 run/jiuwen.log）
     */
    @GetMapping("/download")
    public ResponseEntity<org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody> downloadLog(
            @RequestParam("filename") String filename,
            @RequestParam(name = "source", defaultValue = "kernel") String source) throws java.io.IOException {
        log.info("[RuntimeLogController] downloadLog 入口 filename={} source={}", filename, source);
        permissionChecker.require("log:read");
        // 路径遍历防护：对所有 source 生效
        PathValidator.validate(filename);
        if ("platform".equalsIgnoreCase(source)) {
            return downloadPlatformLog(filename);
        }
        // 流式透传：StreamingResponseBody 在请求线程内同步把上游响应体边读边写进客户端，
        // 写完关闭上游响应，全程不经过 byte[] 堆缓冲。
        final org.springframework.http.client.ClientHttpResponse upstream;
        try {
            upstream = memoryEngineClient.downloadKernelLogs(filename);
        } catch (Exception e) {
            log.error("调用内核 /logs/download?filename={} 失败", filename, e);
            throw new BizException(ResultCode.UPSTREAM_ERROR,
                    "下载运行日志文件失败: " + e.getMessage());
        }
        try {
            log.info("[RuntimeLogController] downloadLog 内核返回 contentLength={}",
                    upstream.getHeaders().getContentLength());
            String downloadName = filename.contains("/")
                    ? filename.substring(filename.lastIndexOf('/') + 1) : filename;
            org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody streamBody =
                    outputStream -> {
                        try (org.springframework.http.client.ClientHttpResponse resp = upstream;
                             java.io.InputStream in = resp.getBody()) {
                            in.transferTo(outputStream);
                            outputStream.flush();
                        }
                    };
            ResponseEntity.BodyBuilder builder = ResponseEntity.ok()
                    .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + downloadName + "\"")
                    .contentType(MediaType.APPLICATION_OCTET_STREAM);
            long len = upstream.getHeaders().getContentLength();
            if (len >= 0) {
                builder.contentLength(len);
            }
            return builder.body(streamBody);
        } catch (Exception e) {
            try { upstream.close(); } catch (Exception ignored) {}
            log.error("下载运行日志文件失败 filename={}", filename, e);
            throw new BizException(ResultCode.UPSTREAM_ERROR,
                    "下载运行日志文件失败: " + e.getMessage());
        }
    }

    /**
     * 下载服务层自身日志文件（platform.log 及轮转 / access log），带目录穿越防护。
     */
    private ResponseEntity<org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody> downloadPlatformLog(String filename) {
        final java.nio.file.Path target;
        try {
            target = platformLogReader.resolveDownload(filename);
        } catch (SecurityException e) {
            throw new BizException(ResultCode.FORBIDDEN, "非法日志文件路径: " + e.getMessage());
        } catch (IllegalArgumentException e) {
            throw new BizException(ResultCode.BAD_REQUEST, e.getMessage());
        } catch (java.io.IOException e) {
            throw new BizException(ResultCode.NOT_FOUND, "日志文件不存在: " + filename);
        }
        try {
            // 流式读文件：Files.newInputStream 边读边写，不一次性 readAllBytes 进堆
            long size = java.nio.file.Files.size(target);
            String downloadName = target.getFileName().toString();
            org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody streamBody =
                    outputStream -> {
                        try (java.io.InputStream in = java.nio.file.Files.newInputStream(target)) {
                            in.transferTo(outputStream);
                            outputStream.flush();
                        }
                    };
            return ResponseEntity.ok()
                    .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + downloadName + "\"")
                    .contentType(MediaType.APPLICATION_OCTET_STREAM)
                    .contentLength(size)
                    .body(streamBody);
        } catch (java.io.IOException e) {
            log.error("读取服务层日志文件失败: {}", filename, e);
            throw new BizException(ResultCode.UPSTREAM_ERROR,
                    "读取服务层日志文件失败: " + e.getMessage());
        }
    }
}
