package com.openjiuwen.memory.common.config;

import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.DefaultMemoryEngineClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

/**
 * 记忆服务 Client 配置：构造 {@link RestClient}（同步），注入 {@code MEMORY_API_KEY} Bearer 鉴权
 * （线上实测：未带 key 调 POST 返回 401）。
 * <p>
 * 注意：与运维中心自身 JWT 鉴权独立——此 key 仅用于调用 :8516。
 */
@Configuration
@ConditionalOnProperty(prefix = "platform.memory-service", name = "mode", havingValue = "real", matchIfMissing = true)
public class WebClientConfig {

    @Value("${platform.memory-service.base-url}")
    private String baseUrl;

    @Value("${platform.memory-service.api-key:}")
    private String apiKey;

    @Value("${platform.memory-service.connect-timeout:5s}")
    private Duration connectTimeout;

    @Value("${platform.memory-service.read-timeout:30s}")
    private Duration readTimeout;

    @Bean
    public SimpleClientHttpRequestFactory memoryRequestFactory() {
        // 独立的 factory Bean：RestClient 与流式下载（手动 createRequest）共用同一套超时配置。
        // SimpleClientHttpRequestFactory 非缓冲，适合大文件流式透传。
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout((int) connectTimeout.toMillis());
        factory.setReadTimeout((int) readTimeout.toMillis());
        return factory;
    }

    @Bean
    public RestClient memoryRestClient(RestClient.Builder builder, SimpleClientHttpRequestFactory memoryRequestFactory) {
        // 注入 Spring Boot 自动配置的 Builder：其消息转换器已带全局 SNAKE_CASE ObjectMapper，
        // 保证 :8516 的 mem_id/scope_id/page_idx 等字段正确反序列化。
        RestClient.Builder b = builder
                .baseUrl(baseUrl)
                .requestFactory(memoryRequestFactory);
        if (apiKey != null && !apiKey.isBlank()) {
            b = b.defaultHeader("Authorization", "Bearer " + apiKey);
        }
        return b.build();
    }

    @Bean
    public MemoryEngineClient memoryEngineClient(RestClient memoryRestClient,
                                                 SimpleClientHttpRequestFactory memoryRequestFactory) {
        return new DefaultMemoryEngineClient(memoryRestClient, memoryRequestFactory, baseUrl, apiKey);
    }
}
