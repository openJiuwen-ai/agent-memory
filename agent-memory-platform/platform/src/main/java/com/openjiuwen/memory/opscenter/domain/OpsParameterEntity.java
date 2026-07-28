package com.openjiuwen.memory.opscenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/**
 * 运维参数本地草稿（功能2）。写回内核经配置中心 SPI；未接入时仅存本地草稿。
 */
@Data
@TableName("ops_parameter")
public class OpsParameterEntity {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String tenantId;
    private String scopeId;
    private String paramKey;
    private String paramValue;
    private String paramType;     // engine/scope/agent/bootstrap/retrieval/dreaming
    private String valueJson;     // 复杂值 JSON
    private Boolean isDraft = true;
    private Instant updatedAt;
    private String updatedBy;
}
