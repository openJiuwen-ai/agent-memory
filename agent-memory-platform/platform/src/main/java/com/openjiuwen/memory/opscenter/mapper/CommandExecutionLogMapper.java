package com.openjiuwen.memory.opscenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.opscenter.domain.CommandExecutionLogEntity;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface CommandExecutionLogMapper extends BaseMapper<CommandExecutionLogEntity> {
    // BaseMapper 提供 selectById/insert/update；按需扩展分页查询
}
