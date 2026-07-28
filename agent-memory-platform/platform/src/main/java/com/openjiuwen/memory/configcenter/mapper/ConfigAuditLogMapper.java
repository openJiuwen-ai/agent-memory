package com.openjiuwen.memory.configcenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.configcenter.domain.ConfigAuditLogEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

/**
 * config_audit_logs 表 Mapper — 配置审计。
 * <p>
 * 2026-07-17 P0-3 v2：使用 {@code operator_id/tenant_id/template_id} 替代 {@code admin_user_id/scope_id}。
 */
@Mapper
public interface ConfigAuditLogMapper extends BaseMapper<ConfigAuditLogEntity> {

    /** 按操作人 + 租户查审计日志（倒序）。tenantId 为空时返回该操作人全部审计。 */
    @Select("SELECT * FROM config_audit_logs WHERE operator_id = #{operatorId} " +
            "AND (tenant_id = #{tenantId} OR #{tenantId} IS NULL) " +
            "ORDER BY operated_at DESC")
    List<ConfigAuditLogEntity> findByOperatorAndTenant(@Param("operatorId") String operatorId,
                                                        @Param("tenantId") String tenantId);

    /** 按操作类型查（用于审计报表）。 */
    @Select("SELECT * FROM config_audit_logs WHERE operation = #{operation} " +
            "ORDER BY operated_at DESC LIMIT #{limit}")
    List<ConfigAuditLogEntity> findByOperation(@Param("operation") String operation,
                                                @Param("limit") int limit);
}
