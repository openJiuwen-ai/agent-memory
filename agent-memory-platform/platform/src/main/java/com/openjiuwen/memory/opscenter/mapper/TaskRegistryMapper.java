package com.openjiuwen.memory.opscenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.opscenter.domain.TaskRegistryEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface TaskRegistryMapper extends BaseMapper<TaskRegistryEntity> {

    @Select("SELECT * FROM task_registry WHERE admin_user_id = #{adminUserId} ORDER BY created_at DESC")
    List<TaskRegistryEntity> findByAdminUserIdOrderByCreatedAtDesc(@Param("adminUserId") String adminUserId);

    @Select("SELECT * FROM task_registry WHERE status = #{status} AND task_type = #{taskType}")
    List<TaskRegistryEntity> findByStatusAndTaskType(@Param("status") String status,
                                                     @Param("taskType") String taskType);

    @Select("SELECT * FROM task_registry WHERE admin_user_id = #{adminUserId} AND status = #{status} AND task_type = #{taskType}")
    List<TaskRegistryEntity> findByAdminUserIdAndStatusAndTaskType(@Param("adminUserId") String adminUserId,
                                                                    @Param("status") String status,
                                                                    @Param("taskType") String taskType);

    @Select("SELECT * FROM task_registry WHERE status = #{status}")
    List<TaskRegistryEntity> findByStatus(@Param("status") String status);
}
