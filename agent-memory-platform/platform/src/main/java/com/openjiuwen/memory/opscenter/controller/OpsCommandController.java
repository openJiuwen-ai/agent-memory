package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.opscenter.domain.CommandExecutionLogEntity;
import com.openjiuwen.memory.opscenter.domain.OpsCommandCatalogEntity;
import com.openjiuwen.memory.opscenter.service.OpsCommandService;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/** 功能1 — 远程发送运维命令。 */
@RestController
@RequestMapping("/api/v1/ops/commands")
public class OpsCommandController {

    private final OpsCommandService service;

    public OpsCommandController(OpsCommandService service) {
        this.service = service;
    }

    @GetMapping
    public ApiResponse<List<OpsCommandCatalogEntity>> catalog(@RequestParam(required = false) String category) {
        return ApiResponse.ok(service.catalog(category));
    }

    @PostMapping("/dispatch")
    public ApiResponse<Map<String, Object>> dispatch(@RequestBody DispatchRequest req) {
        String operator = "system";
        return ApiResponse.ok(service.dispatch(req.commandCode(), req.scopeId(), req.userId(),
                req.payload(), Boolean.TRUE.equals(req.dryRun()), req.reason(), operator));
    }

    @GetMapping("/executions")
    public ApiResponse<PageResult<CommandExecutionLogEntity>> executions(
            @RequestParam(name = "page_idx", defaultValue = "1") int pageIdx,
            @RequestParam(name = "page_size", defaultValue = "20") int pageSize,
            @RequestParam(name = "command_code", required = false) String commandCode,
            @RequestParam(name = "status", required = false) String status) {
        return ApiResponse.ok(service.executions(pageIdx, pageSize, commandCode, status));
    }

    @GetMapping("/executions/{executionId}")
    public ApiResponse<Object> execution(@PathVariable String executionId) {
        return ApiResponse.ok(service.execution(executionId));
    }

    public record DispatchRequest(String commandCode, String scopeId, String userId,
                                  Map<String, Object> payload, Boolean dryRun, String reason) {
    }
}
