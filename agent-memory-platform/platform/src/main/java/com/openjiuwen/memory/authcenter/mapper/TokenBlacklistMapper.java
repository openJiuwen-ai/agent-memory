package com.openjiuwen.memory.authcenter.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.openjiuwen.memory.authcenter.domain.TokenBlacklist;
import org.apache.ibatis.annotations.Mapper;

/**
 * Token 黑名单 Mapper 接口
 */
@Mapper
public interface TokenBlacklistMapper extends BaseMapper<TokenBlacklist> {
}
