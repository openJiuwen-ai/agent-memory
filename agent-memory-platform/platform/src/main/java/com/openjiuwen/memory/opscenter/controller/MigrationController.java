package com.openjiuwen.memory.opscenter.controller;

import com.openjiuwen.memory.common.ApiResponse;
import com.openjiuwen.memory.opscenter.dto.MigrationRequest;
import com.openjiuwen.memory.opscenter.dto.MigrationResultDTO;
import com.openjiuwen.memory.opscenter.service.MigrationService;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/** 功能9 — 数据迁移（§7.7）。 */
@RestController
public class MigrationController {

    private final MigrationService service;

    public MigrationController(MigrationService service) {
        this.service = service;
    }

    /**
     * 执行数据迁移。
     * 调用内核 migrate_between_indices，在两个 BaseMemoryIndex 实例之间批量迁移数据。
     * 迁移是批量复制，源数据保留。迁移过程中服务不可用（730 阶段无在线迁移）。
     */
    @PostMapping("/api/v1/ops/migration")
    public ApiResponse<MigrationResultDTO> migrate(@RequestBody MigrationRequest request) {
        return ApiResponse.ok(service.migrate(request, "system"));
    }
}
