package com.openjiuwen.memory.common;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * 统一响应信封：{@code {code, message, data}}。
 * <p>
 * code=0 成功；非 0 为业务错误码（见 {@link ResultCode}）。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ApiResponse<T>(int code, String message, T data) {

    public static <T> ApiResponse<T> ok(T data) {
        return new ApiResponse<>(0, "ok", data);
    }

    public static <T> ApiResponse<T> ok() {
        return new ApiResponse<>(0, "ok", null);
    }

    public static <T> ApiResponse<T> fail(ResultCode rc) {
        return new ApiResponse<>(rc.code(), rc.message(), null);
    }

    public static <T> ApiResponse<T> fail(int code, String message) {
        return new ApiResponse<>(code, message, null);
    }
}
