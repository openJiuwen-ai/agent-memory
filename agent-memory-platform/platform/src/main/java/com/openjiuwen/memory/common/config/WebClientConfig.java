package com.openjiuwen.memory.common.config;

import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.DefaultMemoryEngineClient;
import org.apache.hc.client5.http.config.ConnectionConfig;
import org.apache.hc.client5.http.config.RequestConfig;
import org.apache.hc.client5.http.impl.classic.CloseableHttpClient;
import org.apache.hc.client5.http.impl.classic.HttpClients;
import org.apache.hc.client5.http.impl.io.PoolingHttpClientConnectionManagerBuilder;
import org.apache.hc.core5.util.Timeout;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.HttpComponentsClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

/**
 * 记忆服务 Client 配置：构造 {@link RestClient}（同步），注入 {@code MEMORY_API_KEY} Bearer 鉴权
 * （线上实测：未带 key 调 POST 返回 401）。
 * <p>
 * 注意：与运维中心自身 JWT 鉴权独立——此 key 仅用于调用 :8516。
 * <p>
 * FIX-007A: 使用 Apache HttpClient 5 连接池替代 SimpleClientHttpRequestFactory（JDK HttpURLConnection）。
 * <p>
 * 原问题（TC-PERF-002 P99=259ms 根因）：SimpleClientHttpRequestFactory 基于 HttpURLConnection，
 * 每次请求新建 TCP 连接（无连接池、无 keep-alive 复用）。50 并发下每请求都经历完整 TCP 握手 +
 * TLS 协商（若 HTTPS），导致 P99 飙升。
 * <p>
 * 修复方案：PoolingHttpClientConnectionManager 维护连接池，复用 keep-alive 连接，
 * 消除重复 TCP 握手开销。池配置：maxTotal=100, maxPerRoute=50（单后端路由足够 50 并发）。
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
    public HttpComponentsClientHttpRequestFactory memoryRequestFactory() {
        // FIX-007A: Apache HttpClient 5 连接池配置
        Timeout connectT = Timeout.ofMilliseconds(connectTimeout.toMillis());
        Timeout socketT = Timeout.ofMilliseconds(readTimeout.toMillis());

        ConnectionConfig connConfig = ConnectionConfig.custom()
                .setConnectTimeout(connectT)
                .setSocketTimeout(socketT)
                .build();

        var connManager = PoolingHttpClientConnectionManagerBuilder.create()
                .setDefaultConnectionConfig(connConfig)
                .setMaxConnTotal(100)
                .setMaxConnPerRoute(50)
                .build();

        RequestConfig requestConfig = RequestConfig.custom()
                .setResponseTimeout(socketT)
                .setConnectTimeout(connectT)
                .build();

        CloseableHttpClient httpClient = HttpClients.custom()
                .setConnectionManager(connManager)
                .setDefaultRequestConfig(requestConfig)
                .build();

        // HttpComponentsClientHttpRequestFactory 非缓冲，适合大文件流式透传（与原 SimpleClientHttpRequestFactory 一致）。
        // createRequest() 方法同样可用（DefaultMemoryEngineClient.downloadKernelLogs 依赖此接口）。
        return new HttpComponentsClientHttpRequestFactory(httpClient);
    }

    @Bean
    public RestClient memoryRestClient(RestClient.Builder builder, HttpComponentsClientHttpRequestFactory memoryRequestFactory) {
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
                                                 HttpComponentsClientHttpRequestFactory memoryRequestFactory) {
        return new DefaultMemoryEngineClient(memoryRestClient, memoryRequestFactory, baseUrl, apiKey);
    }
}
