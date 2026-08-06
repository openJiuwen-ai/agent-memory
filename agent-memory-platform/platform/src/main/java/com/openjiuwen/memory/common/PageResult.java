package com.openjiuwen.memory.common;

import java.util.List;

/**
 * 分页结果。{@code total} 为全局总数；注意 :8516 {@code get_user_mem_by_page} 的 total
 * 实测为当前页条数，Client 层负责纠正（见 DefaultMemoryEngineClient）。
 */
public record PageResult<T>(List<T> items, long total, int pageIdx, int pageSize) {

    public static <T> PageResult<T> of(List<T> items, long total, int pageIdx, int pageSize) {
        return new PageResult<>(items, total, pageIdx, pageSize);
    }
}
