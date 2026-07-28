package com.openjiuwen.memory.logcenter.handler;

import org.apache.ibatis.type.JdbcType;
import org.apache.ibatis.type.MappedTypes;
import org.apache.ibatis.type.TypeHandler;

import java.sql.CallableStatement;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.sql.Types;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;

/**
 * created_at 列的跨库 Instant 映射。
 *
 * 背景：log_collect_records.created_at 在 SQLite 建为 TEXT（V10__log_collect_records_sqlite.sql），
 * 而 MyBatis-Plus 对 Instant 字段默认走 ResultSet#getTimestamp，SQLite JDBC 只能解析
 * "yyyy-MM-dd HH:mm:ss[.SSS]"，遇到应用写入的 ISO-8601（2026-07-23T01:49:00.123Z，含 'T'/'Z'）
 * 直接抛 SQLException("Error parsing time stamp")。
 *
 * 处理策略：
 *  - 写入：用 Timestamp.from(instant)，由驱动按目标列类型自适应（SQLite TEXT 驱动会转字符串）。
 *  - 读取：先 getString 拿原始文本，再同时兼容 ISO-8601（带 T/Z/offset）与
 *    "yyyy-MM-dd HH:mm:ss[.SSS]"（按 UTC 解释）两种格式。
 * MySQL/Gauss 的 TIMESTAMP 列同样安全（驱动对这两种格式都能解析）。
 */
@MappedTypes(Instant.class)
public class InstantTextTypeHandler implements TypeHandler<Instant> {

    private static final DateTimeFormatter LEGACY_FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss[.SSS]");

    @Override
    public void setParameter(PreparedStatement ps, int i, Instant parameter, JdbcType jdbcType) throws SQLException {
        if (parameter == null) {
            ps.setNull(i, jdbcType != null ? jdbcType.TYPE_CODE : Types.OTHER);
        } else {
            ps.setTimestamp(i, Timestamp.from(parameter));
        }
    }

    @Override
    public Instant getResult(ResultSet rs, String columnName) throws SQLException {
        return dbText2Instant(rs.getString(columnName));
    }

    @Override
    public Instant getResult(ResultSet rs, int columnIndex) throws SQLException {
        return dbText2Instant(rs.getString(columnIndex));
    }

    @Override
    public Instant getResult(CallableStatement cs, int columnIndex) throws SQLException {
        return dbText2Instant(cs.getString(columnIndex));
    }

    /**
     * 兼容两种时间格式转 Instant：
     * 1) ISO-8601（含 T/Z/offset，如 2026-07-23T01:49:00.123Z）
     * 2) 传统格式（yyyy-MM-dd HH:mm:ss[.SSS]，按 UTC 解释）
     */
    private static Instant dbText2Instant(String raw) {
        if (raw == null || raw.isBlank()) return null;
        var text = raw.trim();
        try {
            return Instant.parse(text);
        } catch (DateTimeParseException e) {
            return fromLegacyTimestamp(text);
        }
    }

    private static Instant fromLegacyTimestamp(String text) {
        try {
            return LocalDateTime.parse(text, LEGACY_FMT).toInstant(ZoneOffset.UTC);
        } catch (DateTimeParseException e) {
            return null;
        }
    }
}
