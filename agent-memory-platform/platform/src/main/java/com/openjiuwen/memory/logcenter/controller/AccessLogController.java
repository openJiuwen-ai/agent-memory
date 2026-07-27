package com.openjiuwen.memory.logcenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 访问日志查询控制器（V3 §4.6 API#5）。
 * <p>
 * 提供访问日志的瞬时查询接口，读取 AccessLogValve 输出文件。
 * 当前为占位实现，返回空结果（访问日志基础设施待后续迭代补充）。
 */
@RestController
@RequestMapping("/api/v1/logs/access")
public class AccessLogController {

    private final PermissionChecker permissionChecker;

    public AccessLogController(PermissionChecker permissionChecker) {
        this.permissionChecker = permissionChecker;
    }

    /**
     * 瞬时查询访问日志（V3 §4.6 API#5）。
     *
     * @param lines 读取行数（默认500，最大2000）
     * @param level 日志级别过滤（可空）
     */
    @GetMapping("/tail")
    public ApiResponse<Map<String, Object>> tailAccessLogs(
            @RequestParam(name = "lines", defaultValue = "500") int lines,
            @RequestParam(name = "level", required = false) String level) {
        permissionChecker.require("log:read");
        // 访问日志基础设施待后续迭代补充，当前返回空结果
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("lines", List.of());
        result.put("total", 0);
        return ApiResponse.ok(result);
    }
}
