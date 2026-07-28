package com.openjiuwen.memory.configcenter.spi;

import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.exception.GapException;
import com.openjiuwen.memory.common.spi.ConfigCenterClient;
import com.openjiuwen.memory.configcenter.service.KernelConfigService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * ConfigCenterClient SPI 实现 — 配置中心模块接入后覆盖 SpiDefaults 的 noop 占位。
 * <p>
 * 将 SPI 方法委托到配置中心已实现的服务层逻辑：
 * <ul>
 *   <li>getEngineConfig / updateEngineConfig → KernelConfigService（Push 模型）</li>
 *   <li>getScopeConfig / updateScopeConfig → MemoryEngineClient（内核热加载 API）</li>
 *   <li>getAgentConfig / updateAgentConfig → 仍抛 GapException（Agent 策略管理未实现）</li>
 * </ul>
 * <p>
 * 此 @Component 注册后，SpiDefaults 中的 @ConditionalOnMissingBean noopConfigCenterClient
 * 不再生效，运维中心等模块通过 @Autowired ConfigCenterClient 拿到的即此实现。
 */
@Component
public class ConfigCenterClientImpl implements ConfigCenterClient {

    private static final Logger log = LoggerFactory.getLogger(ConfigCenterClientImpl.class);

    private final MemoryEngineClient client;
    private final KernelConfigService kernelConfigService;

    public ConfigCenterClientImpl(MemoryEngineClient client, KernelConfigService kernelConfigService) {
        this.client = client;
        this.kernelConfigService = kernelConfigService;
    }

    /**
     * 获取引擎（内核）配置 — 委托 KernelConfigService。
     * <p>
     * 代理调用内核 GET /admin/config，敏感字段脱敏展示。
     */
    @Override
    public Object getEngineConfig() {
        return kernelConfigService.getKernelConfig();
    }

    /**
     * 更新引擎（内核）配置 — 委托 KernelConfigService。
     * <p>
     * Push 到内核 .env。<b>不自动触发重启</b> — 内部 SPI 调用不经过 Controller 的权限校验，
     * 重启操作必须通过 KernelConfigController（含 config:write + kernel:restart 权限校验 + confirmToken）。
     * 如需重启，调用方应通过 HTTP API 走完整三层安全校验流程。
     */
    @Override
    @SuppressWarnings("unchecked")
    public void updateEngineConfig(Object config) {
        if (config instanceof Map) {
            // 如果传入的是 Map<String, String>，构造 KernelConfigUpdateRequest
            Map<String, String> updates = (Map<String, String>) config;
            com.openjiuwen.memory.configcenter.dto.KernelConfigUpdateRequest request =
                    com.openjiuwen.memory.configcenter.dto.KernelConfigUpdateRequest.builder()
                            .updates(updates)
                            .restart(false)  // 安全加固：SPI 内部调用不触发重启
                            .reason("SPI updateEngineConfig")
                            .build();
            kernelConfigService.updateKernelConfig(request, "spi-config-center");
        } else {
            throw new IllegalArgumentException("updateEngineConfig 需要 Map<String, String> 类型参数");
        }
    }

    /**
     * 获取 Scope 级配置 — 委托 MemoryEngineClient.getScopeConfig。
     * <p>
     * 调用内核 POST /get_scope_config，返回 Map（对应内核 MemoryScopeConfig）。
     */
    @Override
    public Object getScopeConfig(String scopeId) {
        return client.getScopeConfig(scopeId);
    }

    /**
     * 更新 Scope 级配置 — 委托 MemoryEngineClient.setScopeConfig。
     * <p>
     * 调用内核 POST /set_scope_config，热加载即时生效。
     */
    @Override
    @SuppressWarnings("unchecked")
    public void updateScopeConfig(String scopeId, Object config) {
        if (config instanceof Map) {
            client.setScopeConfig(scopeId, (Map<String, Object>) config);
        } else {
            throw new IllegalArgumentException("updateScopeConfig 需要 Map<String, Object> 类型参数");
        }
    }

    /**
     * 获取 Agent 策略配置 — 未实现，抛 GapException。
     */
    @Override
    public Object getAgentConfig(String scopeId) {
        throw new GapException("Agent 策略配置管理未实现，待后续迭代");
    }

    /**
     * 更新 Agent 策略配置 — 未实现，抛 GapException。
     */
    @Override
    public void updateAgentConfig(String scopeId, Object config) {
        throw new GapException("Agent 策略配置管理未实现，待后续迭代");
    }
}
