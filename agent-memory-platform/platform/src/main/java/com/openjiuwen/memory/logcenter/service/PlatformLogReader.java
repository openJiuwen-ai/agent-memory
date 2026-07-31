package com.openjiuwen.memory.logcenter.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * 服务层（platform）运行日志读取器 — 运行日志"服务层"来源（§6.3.2 扩展）。
 * <p>
 * 读取 Spring Boot 落盘的应用日志 {@code ${LOG_DIR}/platform.log}（application.yml logging.file.name），
 * 提供与内核 /logs/tail、/logs/files、/logs/download 等价的能力，供
 * {@code RuntimeLogController?source=platform} 调用。
 * <p>
 * 与内核日志的关系：内核日志由内核自身 /logs/* 提供；本类只负责服务层自身日志，两者互不依赖。
 * <p>
 * 安全：
 * <ul>
 *   <li>tail 用 RandomAccessFile 从尾部读，避免把大文件整体载入内存（OOM 防护）。</li>
 *   <li>download 严格校验目标文件必须位于日志目录内，防止 ../../ 目录穿越。</li>
 *   <li>仅允许 .log 及 .log.*（轮转文件）扩展名。</li>
 * </ul>
 */
@Component
public class PlatformLogReader {

    private static final Logger log = LoggerFactory.getLogger(PlatformLogReader.class);

    /** 服务层日志文件名（与 application.yml logging.file.name 的 basename 一致） */
    private static final String APP_LOG_NAME = "platform.log";
    /** Tomcat access log 文件名前缀（application.yml server.tomcat.accesslog.prefix） */
    private static final String ACCESS_LOG_PREFIX = "localhost_access";
    /** tail 单次最多返回行数（与内核 /logs/tail 上限一致） */
    private static final int MAX_TAIL_LINES = 5000;
    /** 从文件尾部读取时每次回读的字节块大小 */
    private static final int READ_CHUNK = 8192;
    /** 级别锚定匹配：[INFO] / INFO| / INFO: / INFO- / 行首 INFO 空格 */
    private static final Pattern LEVEL_ANCHOR = Pattern.compile("\\[(%s)\\]|\\b%s\\b\\s*[|:\\-]");

    /** 日志目录（与 access log 共用 ${LOG_DIR:./logs}） */
    private final Path logDir;

    public PlatformLogReader(@Value("${LOG_DIR:./logs}") String logDir) {
        this.logDir = Paths.get(logDir).toAbsolutePath().normalize();
    }

    /**
     * 瞬时查询：返回最近 N 行服务层日志（对齐内核 /logs/tail）。
     *
     * @param lines     行数（1..5000）
     * @param level     级别过滤（DEBUG/INFO/WARN/ERROR，可空；WARN 兼容内核 WARNING）
     * @param eventType 事件类型过滤（子串匹配，可空）
     * @return Map 含 lines/total/log_dir；无日志文件时 lines 为空
     */
    public Map<String, Object> tail(int lines, String level, String eventType) {
        int capped = Math.max(1, Math.min(lines, MAX_TAIL_LINES));
        Map<String, Object> result = new LinkedHashMap<>();
        Path appLog = logDir.resolve(APP_LOG_NAME);
        if (!Files.exists(appLog)) {
            result.put("lines", List.of());
            result.put("total", 0);
            result.put("message", "no platform log file found: " + appLog);
            result.put("log_dir", logDir.toString());
            return result;
        }
        try {
            List<String> tailLines = readLastLines(appLog, capped, level, eventType);
            result.put("lines", tailLines);
            result.put("total", tailLines.size());
            result.put("log_dir", logDir.toString());
            return result;
        } catch (IOException e) {
            log.warn("读取服务层日志 tail 失败: {}", e.getMessage());
            result.put("lines", List.of());
            result.put("total", 0);
            result.put("error", e.getMessage());
            result.put("log_dir", logDir.toString());
            return result;
        }
    }

    /**
     * 列出日志目录下可下载的服务层日志文件（对齐内核 /logs/files）。
     * 仅包含服务层自身日志（platform.log 及轮转）与 Tomcat access log，不含其它杂项。
     *
     * @param startDate 开始日期（可空，按修改时间过滤）
     * @param endDate   结束日期（可空）
     * @return 文件项列表，每项含 filename/log_type/size_bytes/size_human/created_at/modified_at/is_rotated
     */
    public List<Map<String, Object>> listFiles(LocalDate startDate, LocalDate endDate) {
        List<Map<String, Object>> result = new ArrayList<>();
        if (!Files.isDirectory(logDir)) {
            return result;
        }
        try (Stream<Path> stream = Files.list(logDir)) {
            List<Path> files = stream
                    .filter(Files::isRegularFile)
                    .filter(this::isPlatformLogFile)
                    .sorted(Comparator.comparing(this::lastModified, Comparator.reverseOrder()))
                    .collect(Collectors.toList());
            for (Path f : files) {
                try {
                    Map<String, Object> item = toFileItem(f);
                    // 日期过滤
                    if (startDate != null || endDate != null) {
                        LocalDate fileDate = Instant.ofEpochMilli(f.toFile().lastModified())
                                .atZone(ZoneId.systemDefault()).toLocalDate();
                        if (startDate != null && fileDate.isBefore(startDate)) continue;
                        if (endDate != null && fileDate.isAfter(endDate)) continue;
                    }
                    result.add(item);
                } catch (Exception e) {
                    // 单个文件读取失败不影响整体
                }
            }
        } catch (IOException e) {
            log.warn("列出服务层日志文件失败: {}", e.getMessage());
        }
        return result;
    }

    /**
     * 按相对文件名解析下载目标（带目录穿越防护，对齐内核 /logs/download）。
     *
     * @param filename 相对日志目录的文件名（由 listFiles 返回的 filename 字段）
     * @return 归一化后的绝对路径
     * @throws SecurityException 目录穿越或非法扩展名
     * @throws IOException       文件不存在
     */
    public Path resolveDownload(String filename) throws SecurityException, IOException {
        // 白名单 + 归一化 + 目录归属校验（统一委托 PathValidator）
        Path target = com.openjiuwen.memory.common.util.PathValidator.validate(filename, logDir);
        String name = target.getFileName().toString();
        if (!(name.endsWith(".log") || name.contains(".log."))) {
            throw new SecurityException("filename '" + filename + "' is not a valid log file");
        }
        if (!Files.exists(target) || !Files.isRegularFile(target)) {
            throw new IOException("log file '" + filename + "' not found");
        }
        return target;
    }

    // ==================== 内部实现 ====================

    /** 判断是否为服务层相关日志文件（应用日志 + access log，含轮转） */
    private boolean isPlatformLogFile(Path p) {
        String name = p.getFileName().toString();
        boolean isApp = name.equals(APP_LOG_NAME) || name.startsWith("platform.") && name.contains(".log");
        boolean isAccess = name.startsWith(ACCESS_LOG_PREFIX) && name.endsWith(".log");
        return isApp || isAccess;
    }

    private long lastModified(Path p) {
        return p.toFile().lastModified();
    }

    private Map<String, Object> toFileItem(Path f) {
        Map<String, Object> item = new LinkedHashMap<>();
        String name = f.getFileName().toString();
        long size = f.toFile().length();
        item.put("filename", name);
        item.put("log_type", name.startsWith(ACCESS_LOG_PREFIX) ? "access" : "common");
        item.put("size_bytes", size);
        item.put("size_human", humanSize(size));
        item.put("created_at", isoTime(f, true));
        item.put("modified_at", isoTime(f, false));
        item.put("is_rotated", !name.equals(APP_LOG_NAME) && name.contains(".log"));
        return item;
    }

    private String isoTime(Path f, boolean created) {
        try {
            long ts = created
                    ? Files.getAttribute(f, "basic:creationTime") instanceof java.nio.file.attribute.FileTime ft
                            ? ft.toMillis() : f.toFile().lastModified()
                    : f.toFile().lastModified();
            return Instant.ofEpochMilli(ts).atZone(ZoneId.systemDefault())
                    .format(DateTimeFormatter.ISO_LOCAL_DATE_TIME);
        } catch (Exception e) {
            return Instant.ofEpochMilli(f.toFile().lastModified()).atZone(ZoneId.systemDefault())
                    .format(DateTimeFormatter.ISO_LOCAL_DATE_TIME);
        }
    }

    private String humanSize(long size) {
        if (size >= 1073741824L) return String.format("%.1f GB", size / 1073741824.0);
        if (size >= 1048576L) return String.format("%.1f MB", size / 1048576.0);
        if (size >= 1024L) return String.format("%.1f KB", size / 1024.0);
        return size + " B";
    }

    /**
     * 从文件尾部读取最近 N 行（带级别/事件过滤）。
     * 为避免 OOM：先取尾部一块原始字节（按平均行长放大），切行后从末尾向前过滤收集。
     */
    private List<String> readLastLines(Path file, int maxLines, String level, String eventType) throws IOException {
        // 预估所需字节：平均行长 ~200B，放大 2 倍安全系数 + 过滤损耗，下限 64KB
        long estimate = Math.max(64 * 1024L, (long) maxLines * 200L * 2);
        long fileSize = Files.size(file);
        long readBytes = Math.min(fileSize, estimate);

        String content;
        try (RandomAccessFile raf = new RandomAccessFile(file.toFile(), "r")) {
            long start = Math.max(0, fileSize - readBytes);
            raf.seek(start);
            byte[] buf = new byte[(int) readBytes];
            raf.readFully(buf);
            // 若从中间开始读，丢弃第一个不完整的行
            content = new String(buf, StandardCharsets.UTF_8);
            if (start > 0) {
                int nl = content.indexOf('\n');
                content = nl >= 0 ? content.substring(nl + 1) : "";
            }
        }

        String[] all = content.split("\n", -1);
        Deque<String> collected = new ArrayDeque<>(maxLines);
        String levelRegex = null;
        if (level != null && !level.isBlank()) {
            String lvl = normalizeLevel(level);
            levelRegex = String.format(LEVEL_ANCHOR.pattern(), lvl, lvl);
        }
        String eventUpper = (eventType == null || eventType.isBlank()) ? null : eventType.toUpperCase();

        // 从末尾向前收集，最多 maxLines 行
        for (int i = all.length - 1; i >= 0 && collected.size() < maxLines; i--) {
            String line = all[i].replace("\r", "");
            if (line.isEmpty()) continue;
            if (levelRegex != null && !Pattern.compile(levelRegex).matcher(line).find()) continue;
            if (eventUpper != null && !line.toUpperCase().contains(eventUpper)) continue;
            collected.addFirst(line);
        }
        return new ArrayList<>(collected);
    }

    /** 兼容内核 WARNING 与服务层 WARN 的级别归一 */
    private String normalizeLevel(String level) {
        String u = level.trim().toUpperCase();
        return "WARNING".equals(u) ? "WARN" : u;
    }
}
