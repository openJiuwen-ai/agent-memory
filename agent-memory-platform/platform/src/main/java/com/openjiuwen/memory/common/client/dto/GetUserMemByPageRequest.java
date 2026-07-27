package com.openjiuwen.memory.common.client.dto;

import lombok.Data;

@Data
public class GetUserMemByPageRequest {

    private String userId = "__default__";
    private String scopeId = "__default__";
    private Integer pageSize = 10;
    private Integer pageIdx = 1;
    /** 小写枚举值，见 {@link MemoryType}；默认 unknown(全部) */
    private String memoryType = MemoryType.UNKNOWN;
}
