package com.openjiuwen.memory.common.spi;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * SPI 缺省实现装配：每个非本模块协作点都提供 @ConditionalOnMissingBean 的占位实现，
 * 确保运维中心可独立编译运行；对应模块接入后以 @Bean 覆盖即可。
 */
@Configuration
public class SpiDefaults {

    private static final Logger log = LoggerFactory.getLogger(SpiDefaults.class);

    @Bean
    @ConditionalOnMissingBean
    public TenantContextProvider noopTenantContextProvider() {
        return () -> null; // identity：未接入多租户时业务代码拿到的即原始 scope/user
    }

    @Bean
    @ConditionalOnMissingBean
    public PermissionChecker noopPermissionChecker() {
        return permission -> {
            log.warn("PermissionChecker 未接入，放行 permission={}", permission);
            return true;
        };
    }

    @Bean
    @ConditionalOnMissingBean
    public ConfirmTokenService noopConfirmTokenService() {
        return new ConfirmTokenService() {
            @Override
            public String issue(String operator, String action, String resource) {
                log.warn("ConfirmTokenService 未接入，签发占位令牌 operator={} action={}", operator, action);
                return "noop-confirm-token";
            }

            @Override
            public boolean validate(String token, String operator, String action, String resource) {
                log.warn("ConfirmTokenService 未接入，放行 token={} action={}", token, action);
                return true;
            }

            @Override
            public void consume(String token) {
                log.warn("ConfirmTokenService 未接入，跳过消费 token={}", token);
            }
        };
    }

    @Bean
    @ConditionalOnMissingBean
    public AuditRecorder noopAuditRecorder() {
        return event -> log.warn("AuditRecorder 未接入，丢弃 event={} {} {}", event.operator(), event.action(), event.resource());
    }

    @Bean
    @ConditionalOnMissingBean
    public ConfigCenterClient noopConfigCenterClient() {
        return new ConfigCenterClient() {
            @Override
            public Object getEngineConfig() {
                throw new com.openjiuwen.memory.common.exception.GapException("配置中心未接入，引擎配置不可读");
            }

            @Override
            public void updateEngineConfig(Object config) {
                throw new com.openjiuwen.memory.common.exception.GapException("配置中心未接入，无法写回引擎配置");
            }
        };
    }

    @Bean
    @ConditionalOnMissingBean
    public TaskCenterClient noopTaskCenterClient() {
        return def -> {
            log.warn("TaskCenterClient 未接入，任务 {} 将同步降级执行", def.type());
            return "sync-" + def.type();
        };
    }

    @Bean
    @ConditionalOnMissingBean
    public MonitoringClient noopMonitoringClient() {
        return new MonitoringClient() {
            @Override
            public void gauge(String name, double value) {
            }

            @Override
            public void increment(String counter) {
            }
        };
    }
}
