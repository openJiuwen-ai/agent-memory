package com.openjiuwen.memory.common;

/**
 * 业务错误码。HTTP 状态由 GlobalExceptionHandler 决定。
 */
public enum ResultCode {

    SUCCESS(0, "ok"),
    BAD_REQUEST(40001, "参数校验失败"),
    FORBIDDEN(40301, "权限不足"),
    NOT_FOUND(40401, "资源不存在"),
    CONFIRM_TOKEN_INVALID(40901, "确认令牌无效或已过期"),
    RATE_LIMITED(42901, "请求过于频繁"),
    UPSTREAM_ERROR(50001, "记忆服务调用失败"),
    GAP(50010, "接口缺口：待记忆服务补齐端点");

    private final int code;
    private final String message;

    ResultCode(int code, String message) {
        this.code = code;
        this.message = message;
    }

    public int code() {
        return code;
    }

    public String message() {
        return message;
    }
}
