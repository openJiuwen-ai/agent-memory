package com.openjiuwen.memory.configcenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.configcenter.domain.ConfigTemplateEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface ConfigTemplateMapper extends BaseMapper<ConfigTemplateEntity> {

    /** 按模板类型查询，预置在前。 */
    @Select("SELECT * FROM config_templates WHERE template_type = #{templateType} ORDER BY is_builtin DESC, template_name")
    List<ConfigTemplateEntity> findByType(String templateType);

    /** 按模板名+类型查（唯一约束）。 */
    @Select("SELECT * FROM config_templates WHERE template_name = #{templateName} AND template_type = #{templateType}")
    ConfigTemplateEntity findByNameAndType(String templateName, String templateType);
}
