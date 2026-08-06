package com.openjiuwen.memory.common.util;

import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;

/**
 * scope_id 格式校验工具 — 与内核 {@code _validate_id} 规则对齐。
 * <p>
 * 规则：
 * <ol>
 *   <li>非空</li>
 *   <li>不含 {@code /}（内核用 / 做 scope 路径分隔，scope_id 内不能出现）</li>
 *   <li>长度不超过 128</li>
 * </ol>
 */
public final class ScopeIdValidator {

    private ScopeIdValidator() {}

    private static final int MAX_LENGTH = 128;

    /**
     * 校验 scope_id 格式，不通过则抛 BizException。
     *
     * @param scopeId 用户传入的 scope_id
     * @throws BizException scope_id 为空、含斜杠、或超长
     */
    public static void validate(String scopeId) {
        if (scopeId == null || scopeId.isBlank()) {
            throw new BizException(ResultCode.BAD_REQUEST, "scope_id 不能为空");
        }
        if (scopeId.contains("/")) {
            throw new BizException(ResultCode.BAD_REQUEST, "scope_id 不能包含斜杠 '/'");
        }
        if (scopeId.length() > MAX_LENGTH) {
            throw new BizException(ResultCode.BAD_REQUEST, "scope_id 长度不能超过 " + MAX_LENGTH);
        }
    }
}
