package com.openjiuwen.memory.common.client.dto;

/**
 * 记忆类型枚举值（对齐记忆服务 MemoryType，线上实测为小写）。
 * <p>
 * 见参考代码 memory_core/manage/mem_model/memory_unit.py：
 * user_profile / semantic_memory / episodic_memory / variable / summary / middle_term_memory / unknown。
 * 服务端 {@code MemoryType(self.memory_type.lower())} 按值匹配，故下传小写值。
 */
public final class MemoryType {

    public static final String UNKNOWN = "unknown";
    public static final String USER_PROFILE = "user_profile";
    public static final String SEMANTIC_MEMORY = "semantic_memory";
    public static final String EPISODIC_MEMORY = "episodic_memory";
    public static final String VARIABLE = "variable";
    public static final String SUMMARY = "summary";
    public static final String MIDDLE_TERM_MEMORY = "middle_term_memory";

    private MemoryType() {
    }
}
