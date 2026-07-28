package com.openjiuwen.memory.configcenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.Instant;

/**
 * 实例级配置 (单例, id=1)
 * <p>
 * 2026-07-17 P0-3 v2 重构：单例表，关联 INSTANCE 模板，修改触发 Kernel Push + 提示重启。
 */
@Data
@TableName("instance_config")
public class InstanceConfigEntity {

    @TableId
    private Integer id;

    private String templateId;

    private String configJson;

    private Integer version;

    private Instant updatedAt;

    private String updatedBy;
}
