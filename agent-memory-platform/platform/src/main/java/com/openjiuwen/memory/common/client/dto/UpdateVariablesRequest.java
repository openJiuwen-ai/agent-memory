package com.openjiuwen.memory.common.client.dto;

import lombok.Data;

import java.util.Map;

@Data
public class UpdateVariablesRequest {

    private Map<String, String> variables;
    private String userId = "__default__";
    private String scopeId = "__default__";
}
