package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.common.client.dto.AddMessagesRequest;
import com.openjiuwen.memory.common.exception.BizException;
import com.openjiuwen.memory.opscenter.domain.FeatureFlagEntity;
import com.openjiuwen.memory.opscenter.mapper.FeatureFlagMapper;
import com.openjiuwen.memory.opscenter.service.impl.FeatureFlagServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.test.util.ReflectionTestUtils;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

/**
 * 功能3 特性配置 Service 单测。覆盖 resolve 合并逻辑（scope>default>fallback）、
 * 未知开关报错、默认 profile 不可删。
 */
@ExtendWith(MockitoExtension.class)
class FeatureFlagServiceImplTest {

    @Mock FeatureFlagMapper mapper;
    @Mock com.openjiuwen.memory.common.spi.PermissionChecker permissionChecker;
    @Mock com.openjiuwen.memory.common.spi.AuditRecorder auditRecorder;

    private FeatureFlagServiceImpl service;

    @BeforeEach
    void setup() {
        service = new FeatureFlagServiceImpl(mapper, permissionChecker, auditRecorder);
        ReflectionTestUtils.setField(service, "defaultScope", "__default__");
        // permissionChecker.check 返回值不被 Service 使用（impl 只调用不判断），无需 stub
    }

    private FeatureFlagEntity profile(String scopeId, boolean enabled, boolean val) {
        FeatureFlagEntity e = new FeatureFlagEntity();
        e.setTenantId("default");
        e.setScopeId(scopeId);
        e.setEnabled(enabled);
        e.setEnableLongTermMem(val);
        e.setEnableUserProfile(val);
        e.setEnableSemanticMemory(val);
        e.setEnableEpisodicMemory(val);
        e.setEnableSummaryMemory(val);
        return e;
    }

    @Test
    void resolve_scopeOverridesDefault() {
        when(mapper.findByTenantIdAndScopeId(anyString(), eq("__default__")))
                .thenReturn(profile("__default__", true, false));   // 默认全关
        when(mapper.findByTenantIdAndScopeId(anyString(), eq("scopeA")))
                .thenReturn(profile("scopeA", true, true));         // scope 全开

        AddMessagesRequest r = service.resolve("scopeA");

        assertThat(r.getEnableLongTermMem()).isTrue();
        assertThat(r.getEnableEpisodicMemory()).isTrue();
        assertThat(r.getEnableSummaryMemory()).isTrue();
    }

    @Test
    void resolve_noScopeFallsBackToDefault() {
        when(mapper.findByTenantIdAndScopeId(anyString(), eq("__default__")))
                .thenReturn(profile("__default__", true, false));
        when(mapper.findByTenantIdAndScopeId(anyString(), eq("scopeA"))).thenReturn(null);

        AddMessagesRequest r = service.resolve("scopeA");

        assertThat(r.getEnableLongTermMem()).isFalse();
        assertThat(r.getEnableUserProfile()).isFalse();
    }

    @Test
    void resolve_neither_returnsFallbackTrue() {
        when(mapper.findByTenantIdAndScopeId(anyString(), anyString())).thenReturn(null);

        AddMessagesRequest r = service.resolve("scopeA");

        assertThat(r.getEnableLongTermMem()).isTrue();
        assertThat(r.getEnableSummaryMemory()).isTrue();
    }

    @Test
    void toggle_unknownFlag_throwsBiz() {
        when(mapper.findByTenantIdAndScopeId(anyString(), eq("scopeA")))
                .thenReturn(profile("scopeA", true, true));
        assertThatThrownBy(() -> service.toggle("scopeA", "bogus", true, "admin"))
                .isInstanceOf(BizException.class);
    }

    @Test
    void delete_defaultScope_throwsBiz() {
        assertThatThrownBy(() -> service.delete("__default__", "admin"))
                .isInstanceOf(BizException.class);
    }

    private static <T> T eq(T value) {
        return org.mockito.ArgumentMatchers.eq(value);
    }
}
