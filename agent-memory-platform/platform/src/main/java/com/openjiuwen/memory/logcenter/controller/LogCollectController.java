package com.openjiuwen.memory.logcenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.logcenter.domain.LogCollectRecordEntity;
import com.openjiuwen.memory.logcenter.mapper.LogCollectRecordMapper;
import com.openjiuwen.memory.logcenter.service.LogCollectAsyncService;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.FileSystemResource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.scheduling.annotation.Scheduled;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.UUID;

/**
 * 日志一键采集控制器（§6.4.4 异步模式）。
 * <p>
 * 三段式异步交互：
 * <ol>
 *   <li>POST /api/v1/logs/collect —— 下发采集任务，立即返回 status=COLLECTING</li>
 *   <li>GET  /api/v1/logs/collect —— 轮询采集记录列表，查看 status 是否变为 READY</li>
 *   <li>GET  /api/v1/logs/collect/{id}/download —— status=READY 后下载采集包</li>
 * </ol>
 * 采集范围最多 7 天（可配置，当前写死）。内核日志按 modified_at 时间范围过滤。
 */
@RestController
@RequestMapping("/api/v1/logs/collect")
public class LogCollectController {

    private static final Logger log = LoggerFactory.getLogger(LogCollectController.class);
    private static final DateTimeFormatter TS_FMT =
            DateTimeFormatter.ofPattern("yyyyMMddHHmmss").withZone(ZoneId.systemDefault());

    private final LogCollectAsyncService asyncService;
    private final PermissionChecker permissionChecker;
    private final LogCollectRecordMapper collectRecordMapper;
    /** 采集包存储目录（与 LogCollectAsyncService 一致，位于 LOG_DIR 下）。 */
    private final String storageDir;

    public LogCollectController(LogCollectAsyncService asyncService,
                                PermissionChecker permissionChecker,
                                LogCollectRecordMapper collectRecordMapper,
                                @Value("${LOG_DIR:./logs}") String logDir) {
        this.asyncService = asyncService;
        this.permissionChecker = permissionChecker;
        this.collectRecordMapper = collectRecordMapper;
        this.storageDir = Paths.get(logDir, "log-collects").toString();
    }

    /**
     * 启动时恢复中断的采集任务：将 COLLECTING 状态的记录标记为 FAILED。
     */
    @PostConstruct
    public void init() {
        asyncService.recoverInterruptedTasks();
    }

    /**
     * 一键采集（异步）：下发采集任务，立即返回 status=COLLECTING。
     * 后台线程异步打包，完成后更新为 READY/FAILED。
     */
    @PostMapping
    public ApiResponse<LogCollectRecordEntity> collectLogs(
            @RequestParam("scene") String scene,
            @RequestParam("start_date") String startDateStr,
            @RequestParam("end_date") String endDateStr,
            @RequestParam(name = "admin_user_id", required = false) String adminUserId,
            @RequestParam(name = "operator_id", required = false) String operatorId,
            @RequestParam(name = "remark", required = false) String remark) {
        permissionChecker.require("log:read");

        // 场景名校验（转拼音/英文便于文件名）
        String sceneSlug = slugify(scene);
        LocalDate startDate;
        LocalDate endDate;
        try {
            startDate = LocalDate.parse(startDateStr);
            endDate = LocalDate.parse(endDateStr);
        } catch (Exception e) {
            throw new BizException(ResultCode.BAD_REQUEST, "日期格式错误，需 yyyy-MM-dd");
        }
        if (startDate.isAfter(endDate)) {
            throw new BizException(ResultCode.BAD_REQUEST, "开始日期不能晚于结束日期");
        }
        long days = ChronoUnit.DAYS.between(startDate, endDate);
        if (days > 7) {
            throw new BizException(ResultCode.BAD_REQUEST, "采集范围不能超过7天");
        }
        String tenant = (adminUserId == null || adminUserId.isBlank()) ? "default" : adminUserId;

        // 生成记录ID和文件名
        String id = UUID.randomUUID().toString().replace("-", "").substring(0, 8);
        String timestamp = TS_FMT.format(Instant.now());
        String name = sceneSlug + "-" + timestamp + "-" + id;

        // 先写 DB 记录（status=COLLECTING），再异步打包
        LogCollectRecordEntity record = new LogCollectRecordEntity();
        record.setId(id);
        record.setScene(scene);
        record.setName(name);
        record.setStartDate(startDateStr);
        record.setEndDate(endDateStr);
        record.setTenantId(tenant);
        record.setFilePath("");
        record.setFileSize(0L);
        record.setStatus("COLLECTING");
        record.setOperatorId(operatorId);
        record.setCreatedAt(Instant.now());
        record.setRemark(remark);
        collectRecordMapper.insert(record);

        // 触发异步打包
        asyncService.executeCollectAsync(record, startDate, endDate);

        return ApiResponse.ok(record);
    }

    /**
     * 列出采集记录（按创建时间倒序）。
     * 前端轮询此接口判断 status 是否变为 READY。
     */
    @GetMapping
    public ApiResponse<List<LogCollectRecordEntity>> listRecords(
            @RequestParam(name = "scene", required = false) String scene,
            @RequestParam(name = "limit", defaultValue = "100") int limit) {
        permissionChecker.require("log:read");
        com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper<LogCollectRecordEntity> wrapper =
                new com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper<>();
        if (scene != null && !scene.isBlank()) {
            wrapper.eq(LogCollectRecordEntity::getScene, scene);
        }
        wrapper.orderByDesc(LogCollectRecordEntity::getCreatedAt);
        wrapper.last("LIMIT " + Math.max(1, Math.min(limit, 500)));
        return ApiResponse.ok(collectRecordMapper.selectList(wrapper));
    }

    /**
     * 查询单个采集记录状态（轮询用）。
     */
    @GetMapping("/{id}")
    public ApiResponse<LogCollectRecordEntity> getRecord(@PathVariable("id") String id) {
        permissionChecker.require("log:read");
        LogCollectRecordEntity record = collectRecordMapper.selectById(id);
        if (record == null) {
            throw new BizException(ResultCode.NOT_FOUND, "采集记录不存在: " + id);
        }
        return ApiResponse.ok(record);
    }

    /**
     * 下载某个采集包（status=READY 后可下载）。
     */
    @GetMapping("/{id}/download")
    public ResponseEntity<FileSystemResource> downloadRecord(@PathVariable("id") String id) {
        permissionChecker.require("log:read");
        LogCollectRecordEntity record = collectRecordMapper.selectById(id);
        if (record == null) {
            throw new BizException(ResultCode.NOT_FOUND, "采集记录不存在: " + id);
        }
        if (!"READY".equals(record.getStatus())) {
            throw new BizException(ResultCode.BAD_REQUEST,
                    "采集包尚未就绪，当前状态: " + record.getStatus());
        }
        Path zipPath = Paths.get(record.getFilePath());
        if (!Files.exists(zipPath)) {
            throw new BizException(ResultCode.NOT_FOUND, "采集包文件已丢失: " + record.getName());
        }
        FileSystemResource resource = new FileSystemResource(zipPath);
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION,
                        "attachment; filename=\"" + record.getName() + ".zip\"")
                .contentType(MediaType.APPLICATION_OCTET_STREAM)
                .body(resource);
    }

    /**
     * 删除某个采集包（文件+记录）。
     */
    @DeleteMapping("/{id}")
    public ApiResponse<Void> deleteRecord(@PathVariable("id") String id) throws IOException {
        permissionChecker.require("ops:write");
        LogCollectRecordEntity record = collectRecordMapper.selectById(id);
        if (record == null) {
            throw new BizException(ResultCode.NOT_FOUND, "采集记录不存在: " + id);
        }
        try {
            if (record.getFilePath() != null && !record.getFilePath().isBlank()) {
                Path zipPath = Paths.get(record.getFilePath());
                Files.deleteIfExists(zipPath);
            }
        } catch (IOException e) {
            log.warn("删除采集包文件失败: {}", e.getMessage());
        }
        collectRecordMapper.deleteById(id);
        return ApiResponse.ok(null);
    }

    /**
     * 过期清理定时任务：按文件 mtime 判断，超过 24h 的采集包文件+DB记录一起删除。
     * 设计文档 §6.4.4：日志需要过期时间，不能一直放在临时目录。
     */
    @Scheduled(fixedDelay = 3600000) // 每小时执行一次
    public void cleanupExpiredCollects() {
        try {
            Path storagePath = Paths.get(storageDir);
            if (!Files.exists(storagePath)) return;
            Instant cutoff = Instant.now().minus(24, ChronoUnit.HOURS);
            List<LogCollectRecordEntity> records = collectRecordMapper.selectList(null);
            for (LogCollectRecordEntity record : records) {
                try {
                    if (record.getFilePath() == null || record.getFilePath().isBlank()) {
                        continue;
                    }
                    Path zipPath = Paths.get(record.getFilePath());
                    if (!Files.exists(zipPath)) {
                        collectRecordMapper.deleteById(record.getId());
                        continue;
                    }
                    long mtimeMillis = zipPath.toFile().lastModified();
                    if (Instant.ofEpochMilli(mtimeMillis).isBefore(cutoff)) {
                        Files.deleteIfExists(zipPath);
                        collectRecordMapper.deleteById(record.getId());
                        log.info("清理过期采集包: {} (mtime={})", record.getName(),
                                Instant.ofEpochMilli(mtimeMillis));
                    }
                } catch (Exception e) {
                    log.warn("清理采集包 {} 失败: {}", record.getId(), e.getMessage());
                }
            }
        } catch (Exception e) {
            log.error("过期采集包清理任务失败", e);
        }
    }

    // ---------- 辅助 ----------

    private String slugify(String scene) {
        if (scene == null || scene.isBlank()) return "collect";
        // 场景中文转英文 slug
        return switch (scene.trim()) {
            case "故障排查" -> "troubleshoot";
            case "日常巡检" -> "inspection";
            case "性能诊断" -> "perf-diag";
            case "上线检查" -> "release-check";
            default -> scene.trim().replaceAll("[^a-zA-Z0-9_-]", "-");
        };
    }
}
