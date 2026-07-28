package com.openjiuwen.memory.common.client.dto;

import lombok.Data;

import java.util.List;

@Data
public class DeleteVariablesRequest {

    private List<String> names;
    private String userId = "__default__";
    private String scopeId = "__default__";
}
