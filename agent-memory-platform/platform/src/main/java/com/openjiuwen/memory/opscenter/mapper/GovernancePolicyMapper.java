package com.openjiuwen.memory.opscenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.opscenter.domain.GovernancePolicyEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface GovernancePolicyMapper extends BaseMapper<GovernancePolicyEntity> {

    /** 取租户策略 + 全局策略（admin_user_id IS NULL）。 */
    @Select("SELECT * FROM governance_policies WHERE admin_user_id = #{adminUserId} OR admin_user_id IS NULL ORDER BY policy_type")
    List<GovernancePolicyEntity> findByAdminUserIdOrGlobal(String adminUserId);

    /** 按 type 取（含全局）。 */
    @Select("SELECT * FROM governance_policies WHERE policy_type = #{policyType} AND (admin_user_id = #{adminUserId} OR admin_user_id IS NULL) ORDER BY admin_user_id")
    List<GovernancePolicyEntity> findByType(String policyType, String adminUserId);
}
