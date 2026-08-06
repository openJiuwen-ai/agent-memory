package com.openjiuwen.memory.scopecenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * Scope注册表实体类
 */
@Data
@TableName("scope_registry")
public class ScopeRegistry {
    
    /**
     * 主键（UUID）
     */
    @TableId(type = IdType.ASSIGN_UUID)
    private String id;
    
    /**
     * Scope ID（全局唯一）
     */
    private String scopeId;
    
    /**
     * Scope名称
     */
    private String scopeName;
    
    /**
     * Scope描述
     */
    private String description;
    
    /**
     * Scope Key（加密存储，仅注册时明文返回一次）
     */
    private String scopeKey;
    
    /**
     * 最大记忆数量配额（0=不限）
     */
    private Integer maxMemories;
    
    /**
     * 状态：unassigned/assigned
     */
    private String status;
    
    /**
     * 分配给的租户ID
     */
    private String assignedToTenantId;
    
    /**
     * 创建时间
     */
    private LocalDateTime createdAt;
    
    /**
     * 更新时间
     */
    private LocalDateTime updatedAt;
}
