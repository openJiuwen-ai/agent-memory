package com.openjiuwen.memory.common.client.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import lombok.Data;

import java.util.List;
import java.util.Map;

/**
 * :8516 原始响应信封（非 {code,data}，Client 层翻译为本模块统一响应）。
 */
public final class RawResponses {

    private RawResponses() {
    }

    /** get_user_mem_by_page 响应。注意：total 实测=当前页条数，非全局总数。 */
    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class GetMemByPageResponse {
        private List<MemoryItem> results;
        private Integer total;
    }

    /** search_memory / search_user_history_summary 响应。 */
    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class SearchResponse {
        private List<MemoryItem> results;
    }

    /** get_variables 响应。 */
    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class GetVariablesResponse {
        private Map<String, String> variables;
    }

    /** delete_variables / delete_mem_by_scope 响应。 */
    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class DeleteResult {
        private String status;
        private Object deleted;
    }

    /** add_messages / update_* 响应：{status, message}。 */
    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class StatusMessage {
        private String status;
        private String message;
    }

    /** /health 响应（浅状态）。 */
    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Health {
        private String status;
        private String message;
    }

    /** get_message_by_id 响应：{found, message_id, role, content, timestamp}。 */
    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class GetMessageResponse {
        private Boolean found;
        private String messageId;
        private String role;
        private String content;
        private String timestamp;
    }
}
