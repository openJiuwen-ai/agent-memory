package com.openjiuwen.memory.common.client.dto;

import lombok.Data;

@Data
public class UpdateMemoryRequest {

    private String memId;
    private String memory;
    private String userId = "__default__";
    private String scopeId = "__default__";
}
