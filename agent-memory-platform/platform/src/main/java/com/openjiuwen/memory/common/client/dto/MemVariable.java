package com.openjiuwen.memory.common.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 变量抽取定义（add_messages.mem_variables 项）。
 * <p>
 * 线上 :8516 用窄模型 MemVariable：name/description 必填，type 限 string/boolean/integer/number。
 * 注意：源码快照用 Param（type/required 必填、支持 array/object），与线上版本不一致——以线上为准。
 */
@Data
public class MemVariable {

    private String name;
    private String description;

    /** string/boolean/integer/number（线上 :8516 仅支持这四种） */
    private String type = "string";

    private Boolean required = true;

    /** "default" 是 Java 关键字，字段名回避并用 @JsonProperty 显式映射 */
    @JsonProperty("default")
    private Object defaultValue;
}
