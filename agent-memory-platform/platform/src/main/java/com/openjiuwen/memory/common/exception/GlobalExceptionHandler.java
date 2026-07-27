package com.openjiuwen.memory.common.exception;
 
import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.ResultCode;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.client.RestClientException;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;

import java.util.Map;

/**
 * 全局异常处理：统一转 {@link ApiResponse} 信封。
 */

@Slf4j
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Map<ResultCode, HttpStatus> BIZ_STATUS_MAP = Map.of(
            ResultCode.FORBIDDEN, HttpStatus.FORBIDDEN,
            ResultCode.NOT_FOUND, HttpStatus.NOT_FOUND,
            ResultCode.CONFIRM_TOKEN_INVALID, HttpStatus.CONFLICT,
            ResultCode.RATE_LIMITED, HttpStatus.TOO_MANY_REQUESTS,
            ResultCode.GAP, HttpStatus.NOT_IMPLEMENTED
    );

    @ExceptionHandler({
            MethodArgumentNotValidException.class,
            MissingServletRequestParameterException.class,
            MethodArgumentTypeMismatchException.class
    })
    public ResponseEntity<ApiResponse<Void>> handleBadRequest(Exception ex) {
        String msg = extractBadRequestMessage(ex);
        return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                .body(ApiResponse.fail(ResultCode.BAD_REQUEST.code(), msg));
    }

    private String extractBadRequestMessage(Exception ex) {
        if (ex instanceof MethodArgumentNotValidException e) {
            return e.getBindingResult().getFieldErrors().stream()
                    .findFirst().map(f -> f.getField() + ": " + f.getDefaultMessage())
                    .orElse(ResultCode.BAD_REQUEST.message());
        }
        if (ex instanceof MissingServletRequestParameterException e) {
            return "缺少必填参数: " + e.getParameterName();
        }
        if (ex instanceof MethodArgumentTypeMismatchException e) {
            return "参数类型错误: " + e.getName() + " 期望 " + e.getRequiredType().getSimpleName();
        }
        return ResultCode.BAD_REQUEST.message();
    }

    @ExceptionHandler(GapException.class)
    public ResponseEntity<ApiResponse<Void>> handleGap(GapException ex) {
        return ResponseEntity.status(HttpStatus.NOT_IMPLEMENTED)
                .body(new ApiResponse<>(ResultCode.GAP.code(), ex.gapHint(), null));
 
 
 
    }
 
    @ExceptionHandler(BizException.class)
    public ResponseEntity<ApiResponse<Void>> handleBiz(BizException ex) {
        ResultCode rc = ex.resultCode();
        HttpStatus status = BIZ_STATUS_MAP.getOrDefault(rc, HttpStatus.BAD_REQUEST);
        return ResponseEntity.status(status).body(ApiResponse.fail(rc.code(), ex.getMessage()));
    }
 
    @ExceptionHandler(RestClientException.class)
    public ResponseEntity<ApiResponse<Void>> handleUpstream(RestClientException ex) {
        log.error("记忆服务调用失败", ex);
        return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
                .body(ApiResponse.fail(ResultCode.UPSTREAM_ERROR.code(), "记忆服务调用失败: " + ex.getMessage()));
    }
 
    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiResponse<Void>> handleOther(Exception ex) {
        log.error("未预期异常", ex);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(ApiResponse.fail(50000, "内部错误: " + ex.getMessage()));
    }
}