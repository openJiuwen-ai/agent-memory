package com.openjiuwen.memory.common.exception;

import com.openjiuwen.memory.common.ResultCode;

/**
 * 接口缺口异常：命中 :8516 未暴露的能力时抛出。
 * <p>
 * 全局异常处理器转为 {@code code=50010}，前端置灰并提示"待记忆服务补齐端点"。
 */
public class GapException extends BizException {

    private final String gapHint;

    public GapException(String gapHint) {
        super(ResultCode.GAP, gapHint);
        this.gapHint = gapHint;
    }

    public String gapHint() {
        return gapHint;
    }
}
