package com.openjiuwen.memory.authcenter.config;

import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * 运维中心前端 (Vue :5173) 与平台后端 (Spring Boot :9000) 分属不同端口，
 * 浏览器会触发跨域预检。本类将 CORS 策略外部化到 application.yml 的
 * {@code platform.cors.*} 节点，避免硬编码，同时按路径前缀分组注册。
 *
 * <p>配置示例（application.yml）：
 * <pre>
 * platform:
 *   cors:
 *     allowed-origins: http://localhost:5173,https://ops.example.com
 *     allowed-methods: GET,POST,PUT,DELETE,OPTIONS
 *     allowed-headers: "*"
 *     allow-credentials: true
 *     max-age: 3600
 * </pre>
 */
@Configuration
public class CorsConfig implements WebMvcConfigurer {

    private static final String PATH_ALL = "/**";

    @Value("${platform.cors.allowed-origins:*}")
    private String allowedOriginsCsv;

    @Value("${platform.cors.allowed-methods:GET,POST,PUT,DELETE,OPTIONS}")
    private String allowedMethodsCsv;

    @Value("${platform.cors.allowed-headers:*}")
    private String allowedHeaders;

    @Value("${platform.cors.allow-credentials:true}")
    private boolean allowCredentials;

    @Value("${platform.cors.max-age:3600}")
    private long maxAge;

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        CorsRegistration registration = registry.addMapping(PATH_ALL);
        registration.allowedOriginPatterns(splitToArray(allowedOriginsCsv))
                    .allowedMethods(splitToArray(allowedMethodsCsv))
                    .allowedHeaders(allowedHeaders)
                    .allowCredentials(allowCredentials)
                    .maxAge(maxAge);
    }

    /**
     * 将逗号分隔的配置值拆分为数组，自动去除首尾空白和空项。
     *
     * @param csv 逗号分隔字符串，如 "GET, POST , DELETE"
     * @return 去空白后的数组；输入为 "*" 时原样返回
     */
    private String[] splitToArray(String csv) {
        if ("*".equals(csv)) {
            return new String[]{"*"};
        }
        List<String> parts = Arrays.stream(csv.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .collect(Collectors.toList());
        return parts.toArray(new String[0]);
    }
}
