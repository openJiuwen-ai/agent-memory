package com.openjiuwen.memory.configcenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.configcenter.domain.ConfirmTokenEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

/**
 * confirm_tokens 表 Mapper — 二次确认令牌。
 */
@Mapper
public interface ConfirmTokenMapper extends BaseMapper<ConfirmTokenEntity> {

    /** 按 token 查。 */
    @Select("SELECT * FROM confirm_tokens WHERE token = #{token}")
    ConfirmTokenEntity findByToken(@Param("token") String token);

    /** 标记已消费（防重放）。 */
    @Update("UPDATE confirm_tokens SET consumed = 1, consumed_at = CURRENT_TIMESTAMP " +
            "WHERE token = #{token} AND consumed = 0")
    int markConsumed(@Param("token") String token);

    /** 按操作人 + action 查所有未消费且未过期的令牌（用于管理后台展示）。 */
    @Select("SELECT * FROM confirm_tokens WHERE operator_id = #{operatorId} AND action = #{action} " +
            "AND consumed = 0 AND expires_at > CURRENT_TIMESTAMP ORDER BY issued_at DESC")
    List<ConfirmTokenEntity> findActiveByOperatorAndAction(@Param("operatorId") String operatorId,
                                                            @Param("action") String action);
}
