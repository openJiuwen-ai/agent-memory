package com.openjiuwen.memory.common.dto;

import lombok.Data;

/**
 * 分页请求 DTO
 */
@Data
public class PageRequest {
    
    /**
     * 页码（从 1 开始）
     */
    private Integer pageNum = 1;
    
    /**
     * 每页大小
     */
    private Integer pageSize = 10;
}
