package com.openjiuwen.memory.configcenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/**
 * 二次确认令牌表（P0-2）— 高危操作二次确认。
 * <p>
 * 流程：issue(签发) → validate(校验) → consume(消费，防重放)
 */
@Data
@TableName("confirm_tokens")
public class ConfirmTokenEntity {

    @TableId(type = IdType.ASSIGN_ID)
    private String token;

    @TableField("operator_id")
    private String operatorId;

    @TableField("action")
    private String action;

    @TableField("resource")
    private String resource;

    @TableField("payload")
    private String payload;

    @TableField("expires_at")
    private Instant expiresAt;

    @TableField("consumed")
    private Boolean consumed;

    @TableField("consumed_at")
    private Instant consumedAt;

    @TableField("issued_at")
    private Instant issuedAt;
}
