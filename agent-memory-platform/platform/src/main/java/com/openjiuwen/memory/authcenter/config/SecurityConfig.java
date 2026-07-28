package com.openjiuwen.memory.authcenter.config;

import com.openjiuwen.memory.authcenter.filter.JwtAuthenticationFilter;
import com.openjiuwen.memory.authcenter.filter.PermissionFilter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

import java.util.Arrays;

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
            @Value("${platform.security.public-paths:/api/v1/auth/login,/api/v1/auth/register,/api/v1/auth/info,/api/v1/auth/logout}")
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
            .addFilterBefore(jwtAuthenticationFilter, UsernamePasswordAuthenticationFilter.class)
            .addFilterAfter(permissionFilter, JwtAuthenticationFilter.class);

        return http.build();
    }
}
