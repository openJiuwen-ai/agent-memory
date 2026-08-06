package com.openjiuwen.memory.common.spi;

/**
 * 操作审计（属"日志中心"模块）。本模块的写操作经此记录。
 */
public interface AuditRecorder {

    void record(AuditEvent event);

    record AuditEvent(String operator, String action, String resource,
                      String status, String requestIp, String detail) {
    }
}
