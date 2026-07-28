package com.openjiuwen.memory.logcenter.spi;

import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.logcenter.domain.OperationLogEntity;
import com.openjiuwen.memory.logcenter.mapper.OperationLogMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.UUID;

/**
 * AuditRecorder SPI 实现 — 覆盖 SpiDefaults 中的 noopAuditRecorder。
 * 将审计事件写入 operation_logs 表。
 */
@Component
public class AuditRecorderImpl implements AuditRecorder {

    private static final Logger log = LoggerFactory.getLogger(AuditRecorderImpl.class);

    private final OperationLogMapper operationLogMapper;
    private final TenantContextProvider tenantContextProvider;

    public AuditRecorderImpl(OperationLogMapper operationLogMapper,
                              TenantContextProvider tenantContextProvider) {
        this.operationLogMapper = operationLogMapper;
        this.tenantContextProvider = tenantContextProvider;
    }

    @Override
    public void record(AuditEvent event) {
        try {
            TenantContextProvider.TenantContext ctx = tenantContextProvider.current();
            String adminUserId = ctx != null && ctx.tenantId() != null ? ctx.tenantId() : "default";
            String operatorId = ctx != null && ctx.userId() != null ? ctx.userId() : event.operator();
            String operatorRole = ctx != null && ctx.role() != null ? ctx.role() : "UNKNOWN";

            OperationLogEntity entity = new OperationLogEntity();
            entity.setId(UUID.randomUUID().toString());
            entity.setAdminUserId(adminUserId);
            entity.setOperatorId(operatorId);
            entity.setOperatorRole(operatorRole);
            entity.setOperationType(event.action());
            entity.setTargetType(parseTargetType(event.resource()));
            entity.setTargetId(event.resource());
            entity.setTargetName(null);
            entity.setRequestMethod(null);
            entity.setRequestPath(event.resource());
            entity.setRequestIp(event.requestIp());
            entity.setRequestBody(null);
            entity.setResponseStatus("SUCCESS".equalsIgnoreCase(event.status()) ? 200 : 500);
            entity.setErrorMessage("SUCCESS".equalsIgnoreCase(event.status()) ? null : event.detail());
            entity.setDurationMs(null);
            entity.setOperatedAt(Instant.now());
            operationLogMapper.insert(entity);
        } catch (Exception e) {
            log.error("Failed to record audit event: {}", event, e);
        }
    }

    private String parseTargetType(String resource) {
        if (resource == null) return "OTHER";
        if (resource.contains("scope")) return "SCOPE";
        if (resource.contains("template")) return "TEMPLATE";
        if (resource.contains("kernel")) return "KERNEL";
        if (resource.contains("memor")) return "MEMORY";
        if (resource.contains("variable")) return "VARIABLE";
        if (resource.contains("dreaming")) return "DREAMING";
        return "OTHER";
    }
}
