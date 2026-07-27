package com.openjiuwen.memory.common.client.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonInclude;
import lombok.Data;

/**
 * 记忆条目。
 * <p>
 * 线上实测：
 * <ul>
 *   <li>get_user_mem_by_page 返回项 = {mem_id, content, type, timestamp, source_id}（无 score）</li>
 *   <li>search_memory / search_user_history_summary 返回项 = {mem_id, content, type, score}</li>
 * </ul>
 * mem_id 为 ULID 格式字符串；type 为小写枚举值（如 summary）。
 */
@Data
@JsonInclude(JsonInclude.Include.NON_NULL)
@JsonIgnoreProperties(ignoreUnknown = true)
public class MemoryItem {

    private String memId;
    private String content;
    private String type;
    /** 仅检索结果携带 */
    private Double score;
    /** 列表回显：所属 user_id（:8516 get_user_mem_by_page 不返，由平台按查询条件回填） */
    private String userId;
    /** 列表回显：所属 scope_id（同上） */
    private String scopeId;
    /** 记忆时间戳（ISO 8601，:8516 get_user_mem_by_page 返回） */
    private String timestamp;
    /** 来源消息 id（:8516 get_user_mem_by_page 返回，可经 get_message_by_id 反查原始对话） */
    private String sourceId;
}
