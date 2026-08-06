package com.openjiuwen.memory.logcenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.baomidou.mybatisplus.core.metadata.IPage;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.openjiuwen.memory.logcenter.domain.OperationLogEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.time.Instant;
import java.util.List;
import java.util.Map;

@Mapper
public interface OperationLogMapper extends BaseMapper<OperationLogEntity> {

    /**
     * 多维度分页查询操作审计日志。
     * 支持按 admin_user_id、operator_id、operation_type、时间范围过滤。
     */
    @Select("""
            <script>
            SELECT * FROM operation_logs
            WHERE admin_user_id = #{adminUserId}
            <if test='operatorId != null and operatorId != ""'>
                AND operator_id = #{operatorId}
            </if>
            <if test='operationType != null and operationType != ""'>
                AND operation_type IN
                <foreach collection='operationType.split(",")' item='t' open='(' separator=',' close=')'>#{t}</foreach>
            </if>
            <if test='successOnly != null and successOnly == true'>
                AND response_status <![CDATA[ < ]]> 400
            </if>
            <if test='startTime != null'>
                AND operated_at <![CDATA[ >= ]]> #{startTime}
            </if>
            <if test='endTime != null'>
                AND operated_at <![CDATA[ <= ]]> #{endTime}
            </if>
            ORDER BY operated_at DESC
            </script>
            """)
    IPage<OperationLogEntity> findPage(Page<OperationLogEntity> page,
                                       @Param("adminUserId") String adminUserId,
                                       @Param("operatorId") String operatorId,
                                       @Param("operationType") String operationType,
                                       @Param("successOnly") Boolean successOnly,
                                       @Param("startTime") Instant startTime,
                                       @Param("endTime") Instant endTime);

    /**
     * 按操作类型统计数量。
     */
    @Select("""
            <script>
            SELECT operation_type AS itemType, COUNT(*) AS count
            FROM operation_logs
            WHERE admin_user_id = #{adminUserId}
            <if test='startTime != null'> AND operated_at <![CDATA[ >= ]]> #{startTime} </if>
            <if test='endTime != null'> AND operated_at <![CDATA[ <= ]]> #{endTime} </if>
            GROUP BY operation_type ORDER BY count DESC
            </script>
            """)
    List<Map<String, Object>> statsByType(@Param("adminUserId") String adminUserId,
                                           @Param("startTime") Instant startTime,
                                           @Param("endTime") Instant endTime);

    /**
     * 按操作人统计数量。
     */
    @Select("""
            <script>
            SELECT operator_id AS itemType, COUNT(*) AS count
            FROM operation_logs
            WHERE admin_user_id = #{adminUserId}
            <if test='startTime != null'> AND operated_at <![CDATA[ >= ]]> #{startTime} </if>
            <if test='endTime != null'> AND operated_at <![CDATA[ <= ]]> #{endTime} </if>
            GROUP BY operator_id ORDER BY count DESC
            </script>
            """)
    List<Map<String, Object>> statsByOperator(@Param("adminUserId") String adminUserId,
                                               @Param("startTime") Instant startTime,
                                               @Param("endTime") Instant endTime);

    /**
     * 统计时间范围内的总操作数。
     */
    @Select("""
            <script>
            SELECT COUNT(*) FROM operation_logs
            WHERE admin_user_id = #{adminUserId}
            <if test='startTime != null'> AND operated_at <![CDATA[ >= ]]> #{startTime} </if>
            <if test='endTime != null'> AND operated_at <![CDATA[ <= ]]> #{endTime} </if>
            </script>
            """)
    long countByAdminAndTimeRange(@Param("adminUserId") String adminUserId,
                                  @Param("startTime") Instant startTime,
                                  @Param("endTime") Instant endTime);

    /**
     * 统计错误率（response_status >= 400 的比例）。
     */
    @Select("""
            <script>
            SELECT CASE WHEN COUNT(*) = 0 THEN 0.0
                        ELSE CAST(SUM(CASE WHEN response_status <![CDATA[ >= ]]> 400 THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) * 100
                   END
            FROM operation_logs
            WHERE admin_user_id = #{adminUserId}
            <if test='startTime != null'> AND operated_at <![CDATA[ >= ]]> #{startTime} </if>
            <if test='endTime != null'> AND operated_at <![CDATA[ <= ]]> #{endTime} </if>
            </script>
            """)
    double calculateErrorRate(@Param("adminUserId") String adminUserId,
                              @Param("startTime") Instant startTime,
                              @Param("endTime") Instant endTime);

    /**
     * 不分页全量查询（CSV 导出用，§6.4.1）。
     */
    @Select("""
            <script>
            SELECT * FROM operation_logs
            WHERE admin_user_id = #{adminUserId}
            <if test='operatorId != null and operatorId != ""'>
                AND operator_id = #{operatorId}
            </if>
            <if test='operationType != null and operationType != ""'>
                AND operation_type IN
                <foreach collection='operationType.split(",")' item='t' open='(' separator=',' close=')'>#{t}</foreach>
            </if>
            <if test='successOnly != null and successOnly == true'>
                AND response_status <![CDATA[ < ]]> 400
            </if>
            <if test='startTime != null'>
                AND operated_at <![CDATA[ >= ]]> #{startTime}
            </if>
            <if test='endTime != null'>
                AND operated_at <![CDATA[ <= ]]> #{endTime}
            </if>
            ORDER BY operated_at DESC
            </script>
            """)
    List<OperationLogEntity> findAllForExport(@Param("adminUserId") String adminUserId,
                                              @Param("operatorId") String operatorId,
                                              @Param("operationType") String operationType,
                                              @Param("successOnly") Boolean successOnly,
                                              @Param("startTime") Instant startTime,
                                              @Param("endTime") Instant endTime);
}
