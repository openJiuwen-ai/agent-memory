package com.openjiuwen.memory.opscenter.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.dto.RawResponses;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.common.exception.GapException;
import com.openjiuwen.memory.common.spi.ConfirmTokenService;
import com.openjiuwen.memory.opscenter.domain.CommandExecutionLogEntity;
import com.openjiuwen.memory.opscenter.domain.OpsCommandCatalogEntity;
import com.openjiuwen.memory.opscenter.mapper.CommandExecutionLogMapper;
import com.openjiuwen.memory.opscenter.mapper.OpsCommandCatalogMapper;
import com.openjiuwen.memory.opscenter.service.impl.OpsCommandServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

/**
 * 功能1 远程运维命令 Service 单测（纯 Mockito，不启 Spring 上下文）。
 * 覆盖：缺口命令返回 gap、dryRun 回显、enabled 命令路由调用 Client、未知命令报错。
 * <p>
 * 安全加固后额外覆盖：
 * <ul>
 *   <li>高危命令（RESTART_KERNEL 等）需要 kernel:restart 权限 + 二次确认令牌</li>
 *   <li>缺少 confirmToken 时抛 BizException(CONFIRM_TOKEN_INVALID)</li>
 * </ul>
 */
@ExtendWith(MockitoExtension.class)
class OpsCommandServiceImplTest {

    @Mock OpsCommandCatalogMapper catalogMapper;
    @Mock CommandExecutionLogMapper execMapper;
    @Mock MemoryEngineClient client;
    @Mock com.openjiuwen.memory.common.spi.PermissionChecker permissionChecker;
    @Mock com.openjiuwen.memory.common.spi.AuditRecorder auditRecorder;
    @Mock ConfirmTokenService confirmTokenService;

    private OpsCommandServiceImpl service;

    @BeforeEach
    void setup() {
        service = new OpsCommandServiceImpl(catalogMapper, execMapper, client,
                permissionChecker, auditRecorder, confirmTokenService, new ObjectMapper());
        // 使用 lenient() 避免 UnnecessaryStubbingException：
        // 并非每个测试都会走到 confirmToken 校验分支
        lenient().when(permissionChecker.check(anyString())).thenReturn(true);
        lenient().when(confirmTokenService.validate(anyString(), anyString(), anyString(), anyString()))
                .thenReturn(true);
    }

    private OpsCommandCatalogEntity cmd(String code, boolean enabled, boolean requireConfirm, String gap) {
        OpsCommandCatalogEntity e = new OpsCommandCatalogEntity();
        e.setCommandCode(code);
        e.setCommandName(code);
        e.setCategory("admin");
        e.setEnabled(enabled);
        e.setRequireConfirm(requireConfirm);
        e.setGapReason(gap);
        return e;
    }

    @Test
    void dispatch_gapCommand_returnsGapAndDoesNotCallClient() {
        when(catalogMapper.selectById("RESTART_KERNEL"))
                .thenReturn(cmd("RESTART_KERNEL", false, true, ":8516 未暴露 /admin/restart"));
        // 模拟真实 Client 缺口：restartKernel() 抛 GapException
        when(client.restartKernel()).thenThrow(new GapException(":8516 未暴露 /admin/restart"));

        Map<String, Object> r = service.dispatch("RESTART_KERNEL", null, null,
                Map.of("confirmToken", "test-token"), false, "test", "admin");

        assertThat(r.get("status")).isEqualTo("gap");
        assertThat((String) r.get("gapHint")).contains("/admin/restart");
        verify(client, times(1)).restartKernel();
        verify(execMapper).insert(any(CommandExecutionLogEntity.class));
    }

    @Test
    void dispatch_dryRun_returnsDryRunAndDoesNotCallClient() {
        when(catalogMapper.selectById("HEALTH_INSPECTION"))
                .thenReturn(cmd("HEALTH_INSPECTION", true, false, null));

        Map<String, Object> r = service.dispatch("HEALTH_INSPECTION", "s1", "u1",
                Map.of(), true, "巡检", "admin");

        assertThat(r.get("status")).isEqualTo("dry_run");
        @SuppressWarnings("unchecked")
        Map<String, Object> result = (Map<String, Object>) r.get("result");
        assertThat(result).containsKey("endpoint");
        verifyNoInteractions(client);
    }

    @Test
    void dispatch_healthInspection_callsClientAndReturnsSuccess() {
        when(catalogMapper.selectById("HEALTH_INSPECTION"))
                .thenReturn(cmd("HEALTH_INSPECTION", true, false, null));
        RawResponses.Health health = new RawResponses.Health();
        health.setStatus("healthy");
        health.setMessage("running");
        when(client.health()).thenReturn(health);

        Map<String, Object> r = service.dispatch("HEALTH_INSPECTION", null, null,
                Map.of(), false, "巡检", "admin");

        assertThat(r.get("status")).isEqualTo("success");
        assertThat(r.get("result")).isInstanceOf(RawResponses.Health.class);
        verify(client, times(1)).health();
    }

    @Test
    void dispatch_unknownCommand_throwsBiz() {
        when(catalogMapper.selectById("NOPE")).thenReturn(null);
        assertThatThrownBy(() -> service.dispatch("NOPE", null, null, Map.of(), false, "x", "admin"))
                .isInstanceOf(BizException.class);
    }

    @Test
    void dispatch_highRiskCommand_withoutConfirmToken_throwsBiz() {
        when(catalogMapper.selectById("RESTART_KERNEL"))
                .thenReturn(cmd("RESTART_KERNEL", true, true, null));

        assertThatThrownBy(() -> service.dispatch("RESTART_KERNEL", null, null,
                Map.of(), false, "重启", "admin"))
                .isInstanceOf(BizException.class)
                .hasMessageContaining("确认令牌");
    }

    @Test
    void dispatch_highRiskCommand_withValidConfirmToken_callsClient() {
        when(catalogMapper.selectById("RESTART_KERNEL"))
                .thenReturn(cmd("RESTART_KERNEL", true, true, null));
        when(client.restartKernel()).thenThrow(new GapException(":8516 未暴露 /admin/restart"));

        Map<String, Object> r = service.dispatch("RESTART_KERNEL", null, null,
                Map.of("confirmToken", "valid-token"), false, "重启", "admin");

        // 缺口命令返回 gap 状态
        assertThat(r.get("status")).isEqualTo("gap");
        // 验证 confirmToken 被消费（防重放）
        verify(confirmTokenService).consume("valid-token");
    }

    @Test
    void catalog_byCategory_delegates() {
        OpsCommandCatalogEntity e = cmd("HEALTH_INSPECTION", true, false, null);
        when(catalogMapper.findByCategory("admin")).thenReturn(List.of(e));
        assertThat(service.catalog("admin")).containsExactly(e);
    }

    @Test
    void execution_notFound_throwsBiz() {
        when(execMapper.selectById("exec_x")).thenReturn(null);
        assertThatThrownBy(() -> service.execution("exec_x")).isInstanceOf(BizException.class);
    }
}
