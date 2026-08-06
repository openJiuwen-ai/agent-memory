package com.openjiuwen.memory.tenantcenter.exception;

import com.openjiuwen.memory.common.CommonResult;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 租户管理模块异常处理
 */
@Slf4j
@RestControllerAdvice(basePackages = "com.openjiuwen.memory.tenantcenter")
public class TenantExceptionHandler {
    
    /**
     * 处理业务异常
     */
    @ExceptionHandler(com.openjiuwen.memory.common.exception.BusinessException.class)
    public CommonResult<Void> handleBusinessException(
            com.openjiuwen.memory.common.exception.BusinessException e) {
        log.error("租户业务异常: {}", e.getMessage());
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
        log.error("租户管理未预期异常", e);
        return CommonResult.error(-1, "服务器内部错误");
    }
}
