package com.openjiuwen.memory.logcenter.filter;

import com.openjiuwen.memory.common.spi.TenantContextProvider;
import com.openjiuwen.memory.logcenter.domain.OperationLogEntity;
import com.openjiuwen.memory.logcenter.mapper.OperationLogMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.util.ContentCachingResponseWrapper;

import java.io.IOException;
import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;

/**
 * 审计日志过滤器：拦截所有API请求，自动记录操作日志。
 * 这是服务层独有能力——内核不记录"谁做了什么"。
 * 使用 Spring OncePerRequestFilter 保证每个请求只执行一次。
 * <p>
 * Fix #4: 使用专用线程池 auditLogExecutor 替代 ForkJoinPool.commonPool()。
 * Fix #15: CallerRunsPolicy 背压策略，队列满时由调用线程同步执行，防止 OOM。
 * Fix #19: parseTargetId 增加路径段数最小值校验。
 */
@Component
public class AuditLogFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(AuditLogFilter.class);

    private final OperationLogMapper operationLogMapper;
    private final TenantContextProvider tenantContextProvider;
    private final Executor auditLogExecutor;

    public AuditLogFilter(OperationLogMapper operationLogMapper,
                          TenantContextProvider tenantContextProvider,
                          @Qualifier("auditLogExecutor") Executor auditLogExecutor) {
        this.operationLogMapper = operationLogMapper;
        this.tenantContextProvider = tenantContextProvider;
        this.auditLogExecutor = auditLogExecutor;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain filterChain)
            throws ServletException, IOException {

        String path = request.getRequestURI();

        // 跳过健康检查和静态资源
        if (path.equals("/health") || path.equals("/") || path.startsWith("/actuator")) {
            filterChain.doFilter(request, response);
            return;
        }

        // 操作日志只记录"写"操作：GET/HEAD/OPTIONS 为只读查询，不产生业务变更，不入 operation_logs
        String httpMethod = request.getMethod();
        if ("GET".equalsIgnoreCase(httpMethod)
                || "HEAD".equalsIgnoreCase(httpMethod)
                || "OPTIONS".equalsIgnoreCase(httpMethod)) {
            filterChain.doFilter(request, response);
            return;
        }

        long startTime = System.currentTimeMillis();

        // 解析请求上下文
        TenantContextProvider.TenantContext ctx = tenantContextProvider.current();
        String adminUserId = ctx != null ? ctx.tenantId() : "default";
        String operatorId = ctx != null && ctx.userId() != null ? ctx.userId() : "system";
        String operatorRole = ctx != null && ctx.role() != null ? ctx.role() : "UNKNOWN";

        // 解析操作类型
        String operationType = parseOperationType(request.getMethod(), path);

        ContentCachingResponseWrapper wrappedResponse = new ContentCachingResponseWrapper(response);

        try {
            filterChain.doFilter(request, wrappedResponse);
        } finally {
            long durationMs = System.currentTimeMillis() - startTime;
            boolean success = wrappedResponse.getStatus() < 400;

            // 异步记录操作审计日志（不阻塞响应）
            // Fix #4: 使用专用线程池，避免 ForkJoinPool.commonPool 耗尽
            // Fix #15: 队列满时 CallerRunsPolicy 背压，防止 OOM
            final String finalAdminUserId = adminUserId;
            final String finalOperatorId = operatorId;
            final String finalOperatorRole = operatorRole;
            final String finalOperationType = operationType;
            final int responseStatus = wrappedResponse.getStatus();
            // Fix: 在请求被 Tomcat 回收前快照 method/ip，避免在 CompletableFuture 里访问已回收的 request
            final String requestMethod = request.getMethod();
            final String requestIp = getClientIp(request);

            CompletableFuture.runAsync(() -> {
                try {
                    OperationLogEntity logEntry = new OperationLogEntity();
                    logEntry.setId(UUID.randomUUID().toString());
                    logEntry.setAdminUserId(finalAdminUserId);
                    logEntry.setOperatorId(finalOperatorId);
                    logEntry.setOperatorRole(finalOperatorRole);
                    logEntry.setOperationType(finalOperationType);
                    logEntry.setTargetType(parseTargetType(path));
                    logEntry.setTargetId(parseTargetId(path));
                    logEntry.setTargetName(null); // 请求体脱敏，暂不记录
                    logEntry.setRequestMethod(requestMethod);
                    logEntry.setRequestPath(path);
                    logEntry.setRequestIp(requestIp);
                    logEntry.setRequestBody(null); // 请求体脱敏，暂不记录
                    logEntry.setResponseStatus(responseStatus);
                    logEntry.setErrorMessage(success ? null : "Request failed with status " + responseStatus);
                    logEntry.setDurationMs((int) durationMs);
                    logEntry.setOperatedAt(Instant.now());
                    operationLogMapper.insert(logEntry);
                } catch (Exception e) {
                    log.error("Failed to record operation audit log", e);
                }
            }, auditLogExecutor);

            // 复制响应体到原始响应
            wrappedResponse.copyBodyToResponse();
        }
    }

    /**
     * 根据HTTP方法和路径解析操作类型（简化版）。
     * 操作类型归类：
     * - CONFIG: 配置相关操作（Scope/Template/Kernel）
     * - MEMORY: 记忆相关操作
     * - VARIABLE: 变量相关操作
     * - DREAMING: 梦境相关操作
     * - USER: 用户认证相关操作
     * - OTHER: 其他操作
     */
    private String parseOperationType(String method, String path) {
        // 配置相关操作
        if (path.contains("/config/scopes") || path.contains("/config/templates") || path.contains("/config/kernel")) {
            if ("DELETE".equalsIgnoreCase(method)) return "CONFIG_DELETE";
            if ("POST".equalsIgnoreCase(method)) return "CONFIG_CREATE";
            if ("PUT".equalsIgnoreCase(method)) return "CONFIG_UPDATE";
        }
        
        // 记忆相关操作
        if (path.contains("/memories")) {
            if ("DELETE".equalsIgnoreCase(method)) return "MEMORY_DELETE";
            if ("PUT".equalsIgnoreCase(method)) return "MEMORY_UPDATE";
            if ("POST".equalsIgnoreCase(method)) return "MEMORY_CREATE";
        }
        
        // 变量相关操作
        if (path.contains("/variables")) {
            if ("DELETE".equalsIgnoreCase(method)) return "VARIABLE_DELETE";
            return "VARIABLE_UPDATE";
        }
        
        // 梦境相关操作
        if (path.contains("/dreaming")) {
            if (path.contains("/start")) return "DREAMING_START";
            if (path.contains("/stop")) return "DREAMING_STOP";
        }
        
        // 用户认证相关操作
        if (path.contains("/auth/login") || path.contains("/login")) return "USER_LOGIN";
        if (path.contains("/auth/logout") || path.contains("/logout")) return "USER_LOGOUT";
        
        return "OTHER";
    }

    /**
     * 根据路径解析目标对象类型。
     */
    private String parseTargetType(String path) {
        if (path.contains("/config/scopes"))
            return "SCOPE";
        if (path.contains("/config/templates"))
            return "TEMPLATE";
        if (path.contains("/config/kernel"))
            return "KERNEL";
        if (path.contains("/memories"))
            return "MEMORY";
        if (path.contains("/variables"))
            return "VARIABLE";
        if (path.contains("/dreaming"))
            return "DREAMING";
        if (path.contains("/auth") || path.contains("/login") || path.contains("/logout"))
            return "USER";
        return "OTHER";
    }

    /**
     * 从路径中提取目标ID（路径最后一段）。
     * Fix #19: 增加路径段数最小值校验，避免操作性路径段误取。
     */
    private String parseTargetId(String path) {
        if (path == null || path.isBlank()) {
            return null;
        }
        String[] parts = path.split("/");
        // 过滤掉空段（路径以 / 开头时第一个元素为空字符串）
        java.util.List<String> segments = new java.util.ArrayList<>();
        for (String part : parts) {
            if (!part.isEmpty()) {
                segments.add(part);
            }
        }
        if (segments.isEmpty()) {
            return null;
        }
        String last = segments.get(segments.size() - 1);
        // 跳过操作性路径段，取倒数第二段
        if (last.equals("rollback") || last.equals("apply") || last.equals("versions")
                || last.equals("start") || last.equals("stop") || last.equals("batch-delete")) {
            if (segments.size() > 1) {
                return segments.get(segments.size() - 2);
            }
            return null;
        }
        return last;
    }

    /**
     * 获取客户端IP。
     */
    private String getClientIp(HttpServletRequest request) {
        String ip = request.getHeader("X-Forwarded-For");
        if (ip == null || ip.isBlank()) {
            ip = request.getHeader("X-Real-IP");
        }
        if (ip == null || ip.isBlank()) {
            ip = request.getRemoteAddr();
        }
        return ip;
    }
}
