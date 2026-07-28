package com.openjiuwen.memory.common.client.dto;

import lombok.Data;

@Data
public class SearchHistorySummaryRequest {

    private String query;
    private Integer num = 10;
    private String userId = "__default__";
    private String scopeId = "__default__";
    private Double threshold = 0.3;
}
