package com.openjiuwen.memory.common;

import lombok.Data;

/**
 * 统一响应结果类
 * @param <T> 数据类型
 */
@Data
public class CommonResult<T> {
    
    /**
     * 响应码：0-成功，其他-失败
     */
    private int code;
    
    /**
     * 响应消息
     */
    private String message;
    
    /**
     * 响应数据
     */
    private T data;
    
    /**
     * 成功响应
     */
    public static <T> CommonResult<T> success(T data) {
        CommonResult<T> result = new CommonResult<>();
        result.setCode(0);
        result.setMessage("success");
        result.setData(data);
        return result;
    }
    
    /**
     * 成功响应（无数据）
     */
    public static <T> CommonResult<T> success() {
        return success(null);
    }
    
    /**
     * 失败响应
     */
    public static <T> CommonResult<T> error(int code, String message) {
        CommonResult<T> result = new CommonResult<>();
        result.setCode(code);
        result.setMessage(message);
        return result;
    }
    
    /**
     * 失败响应（默认错误码）
     */
    public static <T> CommonResult<T> error(String message) {
        return error(-1, message);
    }
}
