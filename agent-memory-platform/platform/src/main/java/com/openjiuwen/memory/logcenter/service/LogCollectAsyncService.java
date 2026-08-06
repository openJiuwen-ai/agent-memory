package com.openjiuwen.memory.logcenter.service;

import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.logcenter.domain.LogCollectRecordEntity;
import com.openjiuwen.memory.logcenter.mapper.LogCollectRecordMapper;
import com.openjiuwen.memory.logcenter.service.MessageLogService;
import com.openjiuwen.memory.logcenter.service.OperationLogService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/**
 * 日志一键采集异步打包服务（§6.4.4 异步模式）。
 * <p>
 * 设计要点：
 * <ul>
 *   <li>POST 下发后立即返回 status=COLLECTING，后台线程异步打包</li>
 *   <li>打包完成 status=READY，失败 status=FAILED</li>
 *   <li>内核日志按时间范围过滤（modified_at 落在 [start, end] 内），最多 7 天</li>
 *   <li>服务重启后可恢复中断的 COLLECTING 任务（标记为 FAILED）</li>
 * </ul>
 */
@Service
public class LogCollectAsyncService {

    private static final Logger log = LoggerFactory.getLogger(LogCollectAsyncService.class);
    /** 采集包存储子目录（位于 LOG_DIR 下，与日志同根，便于统一清理）。 */
    private final String storageDir;
    private static final DateTimeFormatter TS_FMT =
            DateTimeFormatter.ofPattern("yyyyMMddHHmmss").withZone(ZoneId.systemDefault());

    private final MemoryEngineClient memoryEngineClient;
    private final OperationLogService operationLogService;
    private final MessageLogService messageLogService;
    private final LogCollectRecordMapper collectRecordMapper;

    public LogCollectAsyncService(MemoryEngineClient memoryEngineClient,
                                  OperationLogService operationLogService,
                                  MessageLogService messageLogService,
                                  LogCollectRecordMapper collectRecordMapper,
                                  @Value("${LOG_DIR:./logs}") String logDir) {
        this.memoryEngineClient = memoryEngineClient;
        this.operationLogService = operationLogService;
        this.messageLogService = messageLogService;
        this.collectRecordMapper = collectRecordMapper;
        this.storageDir = Paths.get(logDir, "log-collects").toString();
    }

    /**
     * 异步执行打包：内核日志（按时间过滤）+ 操作日志 + 消息日志。
     * 调用方（Controller）已先写入 status=COLLECTING 的 DB 记录，本方法完成后更新为 READY/FAILED。
     * 使用 logCollectExecutor 线程池执行异步任务。
     */
    @Async("logCollectExecutor")
    public void executeCollectAsync(LogCollectRecordEntity record,
                                    LocalDate startDate, LocalDate endDate) {
        String id = record.getId();
        try {
            log.info("开始异步采集打包: id={}, scene={}, range={}~{}", id, record.getScene(), startDate, endDate);
            Path zipPath = doPackage(record, startDate, endDate);

            // 更新记录为 READY
            record.setFilePath(zipPath.toString());
            record.setFileSize(Files.size(zipPath));
            record.setStatus("READY");
            collectRecordMapper.updateById(record);
            log.info("异步采集打包完成: id={}, file={}, size={}", id, zipPath, record.getFileSize());
        } catch (Exception e) {
            log.error("异步采集打包失败: id={}", id, e);
            record.setStatus("FAILED");
            record.setRemark((record.getRemark() == null ? "" : record.getRemark() + " | ")
                    + "采集失败: " + e.getMessage());
            try {
                collectRecordMapper.updateById(record);
            } catch (Exception ex) {
                log.error("更新失败状态失败: id={}", id, ex);
            }
        }
    }

    /**
     * 启动恢复：将重启时仍处于 COLLECTING 状态的记录标记为 FAILED。
     * 由 Controller 在 @PostConstruct 或 ApplicationRunner 中调用。
     */
    public void recoverInterruptedTasks() {
        com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper<LogCollectRecordEntity> wrapper =
                new com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper<>();
        wrapper.eq(LogCollectRecordEntity::getStatus, "COLLECTING");
        List<LogCollectRecordEntity> stuck = collectRecordMapper.selectList(wrapper);
        if (stuck.isEmpty()) return;
        log.warn("发现 {} 个中断的采集任务，标记为 FAILED", stuck.size());
        for (LogCollectRecordEntity r : stuck) {
            r.setStatus("FAILED");
            r.setRemark((r.getRemark() == null ? "" : r.getRemark() + " | ")
                    + "服务重启中断，自动标记失败");
            collectRecordMapper.updateById(r);
        }
    }

    // ---------- 核心打包逻辑 ----------

    /**
     * P0-2 Fix: 流式写入文件，避免 ByteArrayOutputStream 将整个 ZIP 缓存在堆内存导致 OOM。
     * ZipOutputStream 直接包装 FileOutputStream，边打包边落盘。
     */
    private Path doPackage(LogCollectRecordEntity record,
                           LocalDate startDate, LocalDate endDate) throws IOException {
        String name = record.getName();
        String fileName = name + ".zip";

        Path storagePath = Paths.get(storageDir);
        if (!Files.exists(storagePath)) {
            Files.createDirectories(storagePath);
        }
        Path zipPath = storagePath.resolve(fileName);

        Instant startInstant = startDate.atStartOfDay(ZoneId.systemDefault()).toInstant();
        Instant endInstant = endDate.plusDays(1).atStartOfDay(ZoneId.systemDefault()).toInstant();
        String tenant = record.getTenantId();

        // P0-2 Fix: 直接流式写入文件，不再用 ByteArrayOutputStream 缓存全量
        try (java.io.OutputStream fos = Files.newOutputStream(zipPath);
             java.util.zip.CheckedOutputStream cos = new java.util.zip.CheckedOutputStream(fos, new java.util.zip.CRC32());
             ZipOutputStream zos = new ZipOutputStream(cos)) {

            // 内核日志：按时间范围过滤（modified_at 落在 [start, end) 内）
            int idx = 1;
            log.info("[LogCollect] 开始获取内核日志文件列表...");
            List<Map<String, Object>> kernelFiles = memoryEngineClient.listKernelLogFiles();
            log.info("[LogCollect] 内核日志文件列表获取完成，共 {} 个文件", kernelFiles.size());
            for (Map<String, Object> fileInfo : kernelFiles) {
                String kernelFilename = String.valueOf(fileInfo.get("filename"));
                if (!isWithinRange(fileInfo, startInstant, endInstant)) {
                    log.debug("内核日志 {} 不在采集范围内，跳过", kernelFilename);
                    continue;
                }
                log.info("[LogCollect] 开始采集内核日志: {}", kernelFilename);
                collectStreamIntoZip(zos, String.format("%02d-kernel-%s", idx, kernelFilename),
                        () -> memoryEngineClient.downloadKernelLogs(kernelFilename),
                        "内核日志(" + kernelFilename + ")");
                log.info("[LogCollect] 完成采集内核日志: {}", kernelFilename);
                idx++;
            }
            log.info("[LogCollect] 内核日志采集阶段完成，共采集 {} 个文件", idx - 1);

            // 操作日志（服务层 DB，按时间范围）
            log.info("[LogCollect] 开始采集操作日志...");
            collectCsvIntoZip(zos, String.format("%02d-operation-logs-%s-to-%s.csv", idx, startDate, endDate),
                    () -> operationLogService.exportToCsv(tenant, null, null, null, startInstant, endInstant),
                    "操作日志(服务层)");
            log.info("[LogCollect] 操作日志采集完成");
            idx++;

            // 消息日志（服务层 DB，按时间范围）
            log.info("[LogCollect] 开始采集消息日志...");
            collectCsvIntoZip(zos, String.format("%02d-message-logs-%s-to-%s.csv", idx, startDate, endDate),
                    () -> messageLogService.exportToCsv(tenant, null, null, null, startInstant, endInstant),
                    "消息日志(服务层)");
            log.info("[LogCollect] 消息日志采集完成");

            // README
            zos.putNextEntry(new ZipEntry("00-README.txt"));
            zos.write(buildReadme(record.getScene(), startDate, endDate, tenant, name).getBytes(StandardCharsets.UTF_8));
            zos.closeEntry();

            zos.finish();
            zos.flush();
        }
        return zipPath;
    }

    /**
     * 判断内核日志文件是否在采集时间范围内。
     * 优先用 modified_at，回退用 created_at；都没有则纳入（保守采集）。
     */
    private boolean isWithinRange(Map<String, Object> fileInfo, Instant start, Instant end) {
        String modifiedAt = getString(fileInfo, "modified_at");
        String createdAt = getString(fileInfo, "created_at");
        Instant fileTime = null;
        if (modifiedAt != null) {
            fileTime = tryParse(modifiedAt);
        }
        if (fileTime == null && createdAt != null) {
            fileTime = tryParse(createdAt);
        }
        if (fileTime == null) {
            // 无时间信息，保守纳入
            return true;
        }
        return !fileTime.isBefore(start) && fileTime.isBefore(end);
    }

    private static Instant tryParse(String iso) {
        try {
            return Instant.parse(iso);
        } catch (Exception e) {
            try {
                return java.time.LocalDateTime.parse(iso)
                        .atZone(ZoneId.systemDefault()).toInstant();
            } catch (Exception e2) {
                return null;
            }
        }
    }

    private static String getString(Map<String, Object> map, String... keys) {
        for (String k : keys) {
            Object v = map.get(k);
            if (v != null) return String.valueOf(v);
        }
        return null;
    }

    // ---------- 辅助 ----------

    interface ZipBytesSupplier { byte[] get() throws Exception; }
    interface ZipStreamSupplier {
        org.springframework.http.client.ClientHttpResponse get() throws Exception;
    }
    interface CsvSupplier { String get() throws Exception; }

    private void collectIntoZip(ZipOutputStream zos, String entryName, ZipBytesSupplier supplier, String label) {
        try {
            byte[] data = supplier.get();
            if (data != null && data.length > 0) {
                zos.putNextEntry(new ZipEntry(entryName));
                zos.write(data);
                zos.closeEntry();
            }
        } catch (Exception e) {
            log.warn("采集{}失败，跳过: {}", label, e.getMessage());
            writeErrorReadme(zos, entryName.replace(".zip", "-ERROR.txt").replace(".csv", "-ERROR.txt"),
                    label + " 采集失败: " + e.getMessage());
        }
    }

    /**
     * 流式把上游 HTTP 响应体写入 zip：InputStream.transferTo(zos) 边下边压，
     * 内核日志文件全程不进入 JVM 堆（替代旧的 byte[] 全量缓冲）。
     * ClientHttpResponse 由本方法负责 close，确保连接释放。
     */
    private void collectStreamIntoZip(ZipOutputStream zos, String entryName, ZipStreamSupplier supplier, String label) {
        try (org.springframework.http.client.ClientHttpResponse resp = supplier.get()) {
            if (resp != null) {
                zos.putNextEntry(new ZipEntry(entryName));
                try (java.io.InputStream in = resp.getBody()) {
                    in.transferTo(zos);
                }
                zos.closeEntry();
            }
        } catch (Exception e) {
            log.warn("采集{}失败，跳过: {}", label, e.getMessage());
            writeErrorReadme(zos, entryName.replace(".zip", "-ERROR.txt").replace(".csv", "-ERROR.txt"),
                    label + " 采集失败: " + e.getMessage());
        }
    }

    private void collectCsvIntoZip(ZipOutputStream zos, String entryName, CsvSupplier supplier, String label) {
        try {
            String csv = supplier.get();
            if (csv != null && !csv.isEmpty()) {
                zos.putNextEntry(new ZipEntry(entryName));
                zos.write(csv.getBytes(StandardCharsets.UTF_8));
                zos.closeEntry();
            }
        } catch (Exception e) {
            log.warn("采集{}失败，跳过: {}", label, e.getMessage());
            writeErrorReadme(zos, entryName.replace(".csv", "-ERROR.txt"),
                    label + " 采集失败: " + e.getMessage());
        }
    }

    private void writeErrorReadme(ZipOutputStream zos, String entryName, String content) {
        try {
            zos.putNextEntry(new ZipEntry(entryName));
            zos.write(content.getBytes(StandardCharsets.UTF_8));
            zos.closeEntry();
        } catch (IOException ignored) {}
    }

    private String buildReadme(String scene, LocalDate startDate, LocalDate endDate, String tenant, String name) {
        return "=== 日志一键采集包 ===\r\n\r\n"
                + "采集包名称: " + name + "\r\n"
                + "采集场景: " + scene + "\r\n"
                + "采集范围: " + startDate + " 至 " + endDate + "\r\n"
                + "租户ID: " + tenant + "\r\n"
                + "生成时间: " + Instant.now() + "\r\n\r\n"
                + "内容清单:\r\n"
                + "  - NN-kernel-*                内核日志文件(按 modified_at 时间范围过滤，逐个调内核 /logs/download?filename=)\r\n"
                + "  - NN-operation-logs-*.csv   操作审计日志(服务层 DB)\r\n"
                + "  - NN-message-logs-*.csv      消息日志(服务层 DB)\r\n"
                + "  - 过期策略: 采集包保留24小时后自动清理\r\n";
    }
}
