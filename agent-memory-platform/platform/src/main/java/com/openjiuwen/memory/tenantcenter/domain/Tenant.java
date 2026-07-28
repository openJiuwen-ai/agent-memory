package com.openjiuwen.memory.tenantcenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 租户实体类
 */
@Data
@TableName("tenants")
public class Tenant {
    
    /**
     * 租户ID（UUID）
     */
    @TableId(type = IdType.ASSIGN_UUID)
    private String id;
    
    /**
     * 租户名称
     */
    private String name;
    
    /**
     * 状态：active/disabled
     */
    private String status;
    
    /**
     * 创建时间
     */
    private LocalDateTime createdAt;
    
    /**
     * 更新时间
     */
    private LocalDateTime updatedAt;
    
    /**
     * 备注
     */
    private String remark;
    /**
     * 当前生效的 SCOPE 模板 ID（非数据库字段，由业务查询填充）
     */
    @TableField(exist = false)
    private String currentTemplateId;

    /**
     * 当前生效的 SCOPE 模板名（非数据库字段，由业务查询填充）
     */
    @TableField(exist = false)
    private String currentTemplateName;

    /**
     * 租户绑定的 scope_id 列表（兼容旧结构保留 JSON 数组，但当前业务仅允许 1 个）。
     */
    private String scopeIds;
}
