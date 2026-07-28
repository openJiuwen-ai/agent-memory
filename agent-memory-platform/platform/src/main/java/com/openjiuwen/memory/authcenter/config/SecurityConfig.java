package com.openjiuwen.memory.authcenter.config;

import com.openjiuwen.memory.authcenter.filter.JwtAuthenticationFilter;
import com.openjiuwen.memory.authcenter.filter.PermissionFilter;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.security.web.AuthenticationEntryPoint;

import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;

/**
 * 运维中心 Spring Security 配置。
 *
 * <p>认证链路：JWT 过滤器 → 权限过滤器 → 控制器。
 * <ul>
 *   <li>{@link JwtAuthenticationFilter} 从 Authorization 头解析 JWT，
 *       填充 {@code SecurityContextHolder}，位于
 *       {@link UsernamePasswordAuthenticationFilter} 之前。</li>
 *   <li>{@link PermissionFilter} 基于已认证身份做细粒度接口鉴权，
 *       必须在 JWT 过滤器之后执行，否则 authentication 为 null。</li>
 * </ul>
 *
 * <p>公开端点（登录/注册/info/logout）通过 {@code platform.security.public-paths}
 * 配置，逗号分隔，默认覆盖 auth 模块的四个端点。
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

    private final JwtAuthenticationFilter jwtAuthenticationFilter;
    private final PermissionFilter permissionFilter;
    private final String[] publicPaths;

    /**
     * 构造器注入——避免字段注入，便于单元测试与不可变性保证。
     *
     * @param jwtAuthenticationFilter JWT 认证过滤器
     * @param permissionFilter        权限鉴权过滤器
     * @param publicPathsCsv          公开端点 CSV，来自 platform.security.public-paths
     */
    public SecurityConfig(
            JwtAuthenticationFilter jwtAuthenticationFilter,
            PermissionFilter permissionFilter,
            @Value("${platform.security.public-paths:/api/v1/auth/login,/api/v1/auth/register,/api/v1/auth/info,/api/v1/auth/logout,/api/v1/auth/refresh}")
            String publicPathsCsv) {
        this.jwtAuthenticationFilter = jwtAuthenticationFilter;
        this.permissionFilter = permissionFilter;
        this.publicPaths = Arrays.stream(publicPathsCsv.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .toArray(String[]::new);
    }

    @Bean
    public SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        http
            .csrf(csrf -> csrf.disable())
            .sessionManagement(session ->
                session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
            .authorizeHttpRequests(auth -> auth
                .requestMatchers(publicPaths).permitAll()
                .anyRequest().authenticated())
            .exceptionHandling(exceptions -> exceptions
                .authenticationEntryPoint(authenticationEntryPoint()))
            .addFilterBefore(jwtAuthenticationFilter, UsernamePasswordAuthenticationFilter.class)
            .addFilterAfter(permissionFilter, JwtAuthenticationFilter.class);

        return http.build();
    }

    /**
     * 自定义认证入口点：未认证时返回 401（而非默认的 403）
     * 符合 HTTP 语义：401 = 未认证，403 = 已认证但无权限
     */
    @Bean
    public AuthenticationEntryPoint authenticationEntryPoint() {
        return (request, response, authException) -> {
            response.setStatus(HttpStatus.UNAUTHORIZED.value());
            response.setContentType("application/json;charset=UTF-8");
            ObjectMapper mapper = new ObjectMapper();
            Map<String, Object> errorResponse = new HashMap<>();
            errorResponse.put("code", 401);
            errorResponse.put("message", "未认证，请先登录");
            errorResponse.put("data", null);
            String body = mapper.writeValueAsString(errorResponse);
            response.getWriter().write(body);
        };
    }
}
