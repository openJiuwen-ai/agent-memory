package com.openjiuwen.memory.opscenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.opscenter.domain.OpsCommandCatalogEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface OpsCommandCatalogMapper extends BaseMapper<OpsCommandCatalogEntity> {

    @Select("SELECT * FROM ops_command_catalog WHERE category = #{category}")
    List<OpsCommandCatalogEntity> findByCategory(String category);
}
