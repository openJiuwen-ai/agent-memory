package com.openjiuwen.memory.logcenter.service.impl;

import com.baomidou.mybatisplus.core.metadata.IPage;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.openjiuwen.memory.logcenter.domain.OperationLogEntity;
import com.openjiuwen.memory.logcenter.mapper.OperationLogMapper;
import com.openjiuwen.memory.logcenter.service.OperationLogService;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * 操作审计日志服务实现 — 查询和统计操作审计日志。
 */
@Service
public class OperationLogServiceImpl implements OperationLogService {

    private final OperationLogMapper operationLogMapper;

    public OperationLogServiceImpl(OperationLogMapper operationLogMapper) {
        this.operationLogMapper = operationLogMapper;
    }

    @Override
    public IPage<OperationLogEntity> queryLogs(String adminUserId, String operatorId,
                                                String operationType, Boolean successOnly,
                                                Instant startTime, Instant endTime,
                                                int page, int size) {
        String tenant = resolveTenant(adminUserId);
        Page<OperationLogEntity> pageReq = new Page<>(page, size);
        return operationLogMapper.findPage(pageReq, tenant, operatorId, operationType, successOnly, startTime, endTime);
    }

    @Override
    public List<Map<String, Object>> statsByType(String adminUserId, Instant startTime, Instant endTime) {
        return operationLogMapper.statsByType(resolveTenant(adminUserId), startTime, endTime);
    }

    @Override
    public List<Map<String, Object>> statsByOperator(String adminUserId, Instant startTime, Instant endTime) {
        return operationLogMapper.statsByOperator(resolveTenant(adminUserId), startTime, endTime);
    }

    @Override
    public long count(String adminUserId, Instant startTime, Instant endTime) {
        return operationLogMapper.countByAdminAndTimeRange(resolveTenant(adminUserId), startTime, endTime);
    }

    @Override
    public double errorRate(String adminUserId, Instant startTime, Instant endTime) {
        return operationLogMapper.calculateErrorRate(resolveTenant(adminUserId), startTime, endTime);
    }

    @Override
    public String exportToCsv(String adminUserId, String operatorId,
                              String operationType, Boolean successOnly,
                              Instant startTime, Instant endTime) {
        List<OperationLogEntity> rows = operationLogMapper.findAllForExport(
                resolveTenant(adminUserId), operatorId, operationType, successOnly, startTime, endTime);
        StringBuilder sb = new StringBuilder();
        // UTF-8 BOM，确保 Excel 正确识别中文
        sb.append("\uFEFF");
        sb.append("id,admin_user_id,operator_id,operator_role,operation_type,target_type,target_id,target_name,request_method,request_path,request_ip,request_body,response_status,error_message,duration_ms,operated_at\r\n");
        for (OperationLogEntity r : rows) {
            sb.append(csv(r.getId())).append(',')
              .append(csv(r.getAdminUserId())).append(',')
              .append(csv(r.getOperatorId())).append(',')
              .append(csv(r.getOperatorRole())).append(',')
              .append(csv(r.getOperationType())).append(',')
              .append(csv(r.getTargetType())).append(',')
              .append(csv(r.getTargetId())).append(',')
              .append(csv(r.getTargetName())).append(',')
              .append(csv(r.getRequestMethod())).append(',')
              .append(csv(r.getRequestPath())).append(',')
              .append(csv(r.getRequestIp())).append(',')
              .append(csv(r.getRequestBody())).append(',')
              .append(r.getResponseStatus() == null ? "" : r.getResponseStatus()).append(',')
              .append(csv(r.getErrorMessage())).append(',')
              .append(r.getDurationMs() == null ? "" : r.getDurationMs()).append(',')
              .append(r.getOperatedAt() == null ? "" : r.getOperatedAt()).append("\r\n");
        }
        return sb.toString();
    }

    private String csv(String v) {
        if (v == null) return "";
        boolean needQuote = v.contains(",") || v.contains("\"") || v.contains("\n") || v.contains("\r");
        if (needQuote) {
            return "\"" + v.replace("\"", "\"\"") + "\"";
        }
        return v;
    }

    private String resolveTenant(String adminUserId) {
        return adminUserId == null || adminUserId.isBlank() ? "default" : adminUserId;
    }
}
