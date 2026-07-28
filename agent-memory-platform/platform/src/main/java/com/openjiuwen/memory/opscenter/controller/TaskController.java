package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.opscenter.domain.TaskRegistryEntity;
import com.openjiuwen.memory.opscenter.service.TaskService;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/** 功能8 — 任务管理（Dreaming 启动/停止/状态/列表）。 */
@RestController
public class TaskController {

    private final TaskService service;
    private final TenantContextProvider tenantContextProvider;

    public TaskController(TaskService service,
                           TenantContextProvider tenantContextProvider) {
        this.service = service;
        this.tenantContextProvider = tenantContextProvider;
    }

    /** 启动 Dreaming 任务。 */
    @PostMapping("/api/v1/ops/tasks/dreaming/start")
    public ApiResponse<Map<String, Object>> startDreaming(@RequestBody(required = false) Map<String, Object> body,
                                                          @RequestParam(name = "scope_id", required = false) String scopeId,
                                                          @RequestParam(name = "user_id", required = false) String userId) {
        @SuppressWarnings("unchecked")
        Map<String, Object> config = body != null ? (Map<String, Object>) body.get("config") : null;
        return ApiResponse.ok(service.startDreaming(tenantContextProvider.resolveTenant(), scopeId, userId, config, tenantContextProvider.resolveOperator()));
    }

    /** 停止 Dreaming 任务。 */
    @PostMapping("/api/v1/ops/tasks/dreaming/stop")
    public ApiResponse<Map<String, Object>> stopDreaming(@RequestParam(name = "scope_id", required = false) String scopeId,
                                                          @RequestParam(name = "user_id", required = false) String userId) {
        return ApiResponse.ok(service.stopDreaming(tenantContextProvider.resolveTenant(), scopeId, userId, tenantContextProvider.resolveOperator()));
    }

    /** 查询 Dreaming 状态。 */
    @GetMapping("/api/v1/ops/tasks/dreaming/status")
    public ApiResponse<Map<String, Object>> dreamingStatus(@RequestParam(name = "scope_id", required = false) String scopeId,
                                                            @RequestParam(name = "user_id", required = false) String userId) {
        return ApiResponse.ok(service.dreamingStatus(tenantContextProvider.resolveTenant(), scopeId, userId));
    }

    /** 查询任务列表。 */
    @GetMapping("/api/v1/ops/tasks")
    public ApiResponse<List<TaskRegistryEntity>> listTasks() {
        return ApiResponse.ok(service.listTasks(tenantContextProvider.resolveTenant()));
    }

    /** 查询单个任务详情。 */
    @GetMapping("/api/v1/ops/tasks/{taskId}")
    public ApiResponse<TaskRegistryEntity> getTask(@PathVariable String taskId) {
        return ApiResponse.ok(service.getTask(taskId));
    }
}
