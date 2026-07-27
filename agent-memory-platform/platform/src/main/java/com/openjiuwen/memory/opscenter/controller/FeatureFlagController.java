package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.client.dto.AddMessagesRequest;
import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.opscenter.domain.FeatureFlagEntity;
import com.openjiuwen.memory.opscenter.service.FeatureFlagService;
import org.springframework.web.bind.annotation.*;

import java.util.List;

/** 功能3 — 特性配置（enable_* 五元组）。 */
@RestController
@RequestMapping("/api/v1/ops/features")
public class FeatureFlagController {

    private final FeatureFlagService service;

    public FeatureFlagController(FeatureFlagService service) {
        this.service = service;
    }

    @GetMapping
    public ApiResponse<List<FeatureFlagEntity>> list() {
        return ApiResponse.ok(service.list());
    }

    @GetMapping("/{scopeId}")
    public ApiResponse<FeatureFlagService.FeatureView> get(@PathVariable String scopeId) {
        return ApiResponse.ok(service.get(scopeId));
    }

    @PutMapping("/{scopeId}")
    public ApiResponse<Void> upsert(@PathVariable String scopeId, @RequestBody AddMessagesRequest flags) {
        String operator = "system"; // TODO: 从 JWT 取
        service.upsert(scopeId, flags, operator);
        return ApiResponse.ok();
    }

    @PatchMapping("/{scopeId}/toggle")
    public ApiResponse<Void> toggle(@PathVariable String scopeId, @RequestBody ToggleRequest req) {
        String operator = "system";
        service.toggle(scopeId, req.flag(), req.value(), operator);
        return ApiResponse.ok();
    }

    @DeleteMapping("/{scopeId}")
    public ApiResponse<Void> delete(@PathVariable String scopeId) {
        String operator = "system";
        service.delete(scopeId, operator);
        return ApiResponse.ok();
    }

    public record ToggleRequest(String flag, boolean value) {
    }
}
