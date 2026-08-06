package com.openjiuwen.memory.authcenter.exception;

import com.openjiuwen.memory.common.CommonResult;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 租户与用户管理模块异常处理
 */
@Slf4j
@RestControllerAdvice(basePackages = "com.openjiuwen.memory.authcenter")
public class AuthExceptionHandler {
    
    /**
     * 处理参数校验异常
     */
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public CommonResult<Void> handleValidation(MethodArgumentNotValidException ex) {
        String msg = ex.getBindingResult().getFieldErrors().stream()
                .findFirst().map(e -> e.getField() + ": " + e.getDefaultMessage())
                .orElse("参数校验失败");
        log.error("参数校验失败: {}", msg);
        return CommonResult.error(-2, msg);
    }
    
    /**
     * 处理业务异常
     */
    @ExceptionHandler(com.openjiuwen.memory.common.exception.BusinessException.class)
    public CommonResult<Void> handleBusinessException(
            com.openjiuwen.memory.common.exception.BusinessException e) {
        log.error("业务异常: {}", e.getMessage());
        return CommonResult.error(e.getCode(), e.getMessage());
    }
    
    /**
     * 处理非法参数异常
     */
    @ExceptionHandler(IllegalArgumentException.class)
    public CommonResult<Void> handleIllegalArgumentException(IllegalArgumentException e) {
        log.error("非法参数: {}", e.getMessage());
        return CommonResult.error(-2, e.getMessage());
    }
    
    /**
     * 处理其他异常
     */
    @ExceptionHandler(Exception.class)
    public CommonResult<Void> handleException(Exception e) {
        log.error("未预期异常", e);
        return CommonResult.error(-1, "服务器内部错误");
    }
}
