package com.openjiuwen.memory.common.exception;

import com.openjiuwen.memory.common.ResultCode;

/**
 * 业务异常，携带错误码。
 */
public class BizException extends RuntimeException {

    private final ResultCode resultCode;

    public BizException(ResultCode rc) {
        super(rc.message());
        this.resultCode = rc;
    }

    public BizException(ResultCode rc, String message) {
        super(message);
        this.resultCode = rc;
    }

    public ResultCode resultCode() {
        return resultCode;
    }
}
