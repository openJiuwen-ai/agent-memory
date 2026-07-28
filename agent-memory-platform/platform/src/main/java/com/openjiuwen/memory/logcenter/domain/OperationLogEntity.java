package com.openjiuwen.memory.logcenter.domain;

import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/**
 * 操作审计日志表 — 服务层独有，内核不提供。
 * 记录所有管理操作的审计轨迹：谁(WHO)做了什么(WHAT)对哪个对象(TARGET)什么时候(WHEN)结果如何(RESULT)。
 * operation_type: CONFIG_CREATE/CONFIG_UPDATE/CONFIG_DELETE/CONFIG_ROLLBACK/CONFIG_TEMPLATE_APPLY/
 *                 MEMORY_ADD/MEMORY_UPDATE/MEMORY_DELETE/MEMORY_DELETE_BY_SCOPE/MEMORY_DELETE_BY_USER/
 *                 VARIABLE_UPDATE/VARIABLE_DELETE/DREAMING_START/DREAMING_STOP/SCOPE_CREATE/SCOPE_DELETE/
 *                 USER_LOGIN/USER_LOGOUT/OTHER
 */
@Data
@TableName("operation_logs")
public class OperationLogEntity {

    @TableId
    private String id;

    private String adminUserId;
    private String operatorId;
    private String operatorRole;
    private String operationType;
    private String targetType;
    private String targetId;
    private String targetName;
    private String requestMethod;
    private String requestPath;
    private String requestIp;
    /** 请求参数(脱敏, JSON字符串) */
    private String requestBody;
    private Integer responseStatus;
    private String errorMessage;
    private Integer durationMs;
    private Instant operatedAt;
}
