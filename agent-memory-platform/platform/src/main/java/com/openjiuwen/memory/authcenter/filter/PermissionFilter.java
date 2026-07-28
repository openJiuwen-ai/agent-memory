package com.openjiuwen.memory.authcenter.filter;

import com.openjiuwen.memory.authcenter.service.RolePermissionService;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.GrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.util.AntPathMatcher;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 基于角色的权限拦截器
 * 根据用户的角色和请求路径，判断是否有权限访问
 */
@Slf4j
@Component
public class PermissionFilter extends OncePerRequestFilter {
    
    @Autowired
    private RolePermissionService rolePermissionService;
    
    private final AntPathMatcher pathMatcher = new AntPathMatcher();
    
    /**
     * 权限配置映射
     * key: URL 路径模式
     * value: 需要的权限
     */
    private static final Map<String, String> PERMISSION_MAP = new HashMap<String, String>() {{
        // 租户管理 - 租户列表需要 tenant:read
        put("/api/v1/tenants", "tenant:read");
        put("/api/v1/tenants/**", "tenant:read");
        put("POST:/api/v1/tenants", "tenant:write");
        put("PUT:/api/v1/tenants/**", "tenant:write");
        put("DELETE:/api/v1/tenants/**", "tenant:write");
        
        // Scope管理 - Scope列表需要 scope:read（VIEWER可以查看）
        put("/api/v1/scopes", "scope:read");
        put("/api/v1/scopes/**", "scope:read");
        put("POST:/api/v1/scopes", "scope:write");
        put("PUT:/api/v1/scopes/**", "scope:write");
        put("DELETE:/api/v1/scopes/**", "scope:write");
        
        // 用户管理
        put("/api/v1/users", "user:read");
        put("/api/v1/users/**", "user:read");
        put("POST:/api/v1/users", "user:write");
        put("PUT:/api/v1/users/**", "user:write");
        put("DELETE:/api/v1/users/**", "user:write");
        
        // 角色管理
        put("/api/v1/roles", "user:read");
        
        // 配置中心（§5）
        put("/api/v1/config/**", "config:read");
        put("POST:/api/v1/config/**", "config:write");
        put("PUT:/api/v1/config/**", "config:write");
        put("DELETE:/api/v1/config/**", "config:write");
        
        // 运维中心（§7）- 记忆查询需要 memory:read，其他运维操作需要 ops:read
        put("/api/v1/ops/memory", "memory:read");  // GET 记忆列表
        put("/api/v1/ops/memory/**", "memory:read");  // GET 记忆详情
        put("POST:/api/v1/ops/memory", "memory:write");  // POST 创建记忆
        put("PUT:/api/v1/ops/memory/**", "memory:write");  // PUT 更新记忆
        put("DELETE:/api/v1/ops/memory/**", "memory:delete");  // DELETE 删除记忆
        put("DELETE:/api/v1/ops/memory", "memory:delete");  // DELETE 批量删除
        put("/api/v1/ops/tasks/**", "ops:read");  // 任务管理
        put("POST:/api/v1/ops/tasks/**", "ops:write");
        
        // 日志中心（§6）
        put("/api/v1/logs/**", "log:read");
        
        // 记忆管理
        put("/api/v1/memories/**", "memory:read");
        put("POST:/api/v1/memories/**", "memory:write");
        put("PUT:/api/v1/memories/**", "memory:write");
        put("DELETE:/api/v1/memories/**", "memory:write");
    }};
    
    @Override
    protected void doFilterInternal(HttpServletRequest request, 
                                   HttpServletResponse response, 
                                   FilterChain filterChain) throws ServletException, IOException {
        String uri = request.getRequestURI();
        String method = request.getMethod();
        
        // 获取当前用户信息
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication == null || !authentication.isAuthenticated()) {
            filterChain.doFilter(request, response);
            return;
        }
        
        // 获取用户角色
        Collection<? extends GrantedAuthority> authorities = authentication.getAuthorities();
        if (authorities.isEmpty()) {
            filterChain.doFilter(request, response);
            return;
        }
        
        // 获取角色名称（去掉 ROLE_ 前缀）
        String role = authorities.iterator().next().getAuthority();
        if (role.startsWith("ROLE_")) {
            role = role.substring(5);
        }

        // 检查权限
        String requiredPermission = getRequiredPermission(method, uri);
        log.info("[PermissionFilter] user={} role={} method={} uri={} requiredPermission={}",
                authentication.getName(), role, method, uri, requiredPermission);
        if (requiredPermission != null) {
            // 查询该角色是否有此权限
            List<String> permissions = rolePermissionService.getPermissionsByRole(role);
            log.info("[PermissionFilter] user={} role={} permissions={} required={} hasPerm={}",
                    authentication.getName(), role, permissions, requiredPermission,
                    permissions.contains(requiredPermission));
            if (!permissions.contains(requiredPermission)) {
                log.warn("[PermissionFilter] 用户 {} 角色 {} 没有权限 {} 访问 {}",
                    authentication.getName(), role, requiredPermission, uri);
                response.setStatus(HttpServletResponse.SC_FORBIDDEN);
                response.setContentType("application/json;charset=UTF-8");
                response.getWriter().write(
                    String.format("{\"code\":403,\"message\":\"没有权限访问\",\"data\":null}")
                );
                return;
            }
        }
        
        filterChain.doFilter(request, response);
    }
    
    /**
     * 根据请求方法和路径获取需要的权限
     */
    private String getRequiredPermission(String method, String uri) {
        // 先尝试匹配带方法前缀的规则
        String methodPath = method + ":" + uri;
        for (Map.Entry<String, String> entry : PERMISSION_MAP.entrySet()) {
            if (pathMatcher.match(entry.getKey(), methodPath)) {
                return entry.getValue();
            }
        }
        
        // 再尝试匹配不带方法前缀的规则
        for (Map.Entry<String, String> entry : PERMISSION_MAP.entrySet()) {
            if (!entry.getKey().contains(":") && pathMatcher.match(entry.getKey(), uri)) {
                return entry.getValue();
            }
        }
        
        return null;
    }
}
