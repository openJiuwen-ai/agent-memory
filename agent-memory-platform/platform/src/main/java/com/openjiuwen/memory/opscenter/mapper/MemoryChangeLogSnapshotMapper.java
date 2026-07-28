package com.openjiuwen.memory.opscenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.opscenter.domain.MemoryChangeLogSnapshotEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.time.Instant;
import java.util.List;

@Mapper
public interface MemoryChangeLogSnapshotMapper extends BaseMapper<MemoryChangeLogSnapshotEntity> {

    @Select("SELECT * FROM memory_change_log_snapshot WHERE mem_id = #{memId} ORDER BY created_at ASC")
    List<MemoryChangeLogSnapshotEntity> findByMemIdOrderByCreatedAtAsc(String memId);

    /** 统计指定变更类型的变更数量。 */
    @Select("SELECT COUNT(*) FROM memory_change_log_snapshot WHERE change_type = #{changeType}")
    long countByChangeType(@Param("changeType") String changeType);

    /** 统计指定时间范围内、指定变更类型的变更数量。 */
    @Select("SELECT COUNT(*) FROM memory_change_log_snapshot WHERE change_type = #{changeType} AND created_at >= #{startTime}")
    long countByChangeTypeAndCreatedAtAfter(@Param("changeType") String changeType,
                                             @Param("startTime") Instant startTime);

    /** 查询指定时间之后的全部快照，按创建时间升序返回。 */
    @Select("SELECT * FROM memory_change_log_snapshot WHERE created_at >= #{startTime} ORDER BY created_at ASC")
    List<MemoryChangeLogSnapshotEntity> findByCreatedAtAfter(@Param("startTime") Instant startTime);
}
