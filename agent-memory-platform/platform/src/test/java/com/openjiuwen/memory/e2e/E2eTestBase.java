package com.openjiuwen.memory.e2e;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.authcenter.config.JwtTokenProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.*;
import org.springframework.test.context.ActiveProfiles;

/**
 * E2E 测试基类 — 提供 JWT 认证辅助方法和 HTTP 请求工具。
 * <p>
 * 所有 E2E 测试类继承此基类，共享：
 * <ul>
 *   <li>{@link TestRestTemplate} — 随机端口的 HTTP 客户端</li>
 *   <li>{@link JwtTokenProvider} — 直接生成 JWT token（绕过登录，测试过滤器链）</li>
 *   <li>便捷方法：authHeader、get/post/put/delete with token</li>
 * </ul>
 * <p>
 * 测试 Profile：e2e（SQLite + Mock 内核 + 全量迁移 V1~V10）
 * <p>
 * <b>已知系统行为（E2E 测试据此编写）：</b>
 * <ul>
 *   <li>PermissionFilter 在 JwtAuthenticationFilter 之前执行（SecurityConfig L52），
 *       导致 PermissionFilter 检查 SecurityContextHolder 时 authentication 始终为 null，
 *       权限校验被完全绕过。所有角色（含 READ_ONLY）均可访问受保护端点。</li>
 *   <li>SPI PermissionChecker 为 noop（SpiDefaults），controller 层 permissionChecker.require() 不生效。</li>
 *   <li>PERMISSION_MAP 无 /api/v1/scopes/** 和 /api/v1/auth/** 条目，这些路径完全跳过权限检查。</li>
 *   <li>CommonResult 用于 AuthController 和 ScopeRegistryController（code: 0=success, -1=error）。</li>
 *   <li>ApiResponse 用于其他所有 controller（code: 0=success, 非0=error）。</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("e2e")
public abstract class E2eTestBase {

    @Autowired
    protected TestRestTemplate restTemplate;

    @Autowired
    protected JwtTokenProvider jwtTokenProvider;

    @Autowired
    protected ObjectMapper objectMapper;

    // ==================== 种子数据常量 ====================

    /** V6 迁移脚本种子管理员用户 ID */
    protected static final String SEED_ADMIN_USER_ID = "user_admin";
    /** V6 迁移脚本种子管理员用户名 */
    protected static final String SEED_ADMIN_USERNAME = "admin";
    /** V6 迁移脚本种子管理员密码（BCrypt 明文） */
    protected static final String SEED_ADMIN_PASSWORD = "admin123";
    /** V6 迁移脚本种子租户 ID */
    protected static final String SEED_TENANT_ID = "tenant_default";
    /** V7 迁移脚本种子 Scope ID（scope_01 ~ scope_10） */
    protected static final String SEED_SCOPE_01 = "scope_01";
    protected static final String SEED_SCOPE_02 = "scope_02";

    // ==================== JWT Token 生成 ====================

    /**
     * 为指定角色生成 JWT token。
     * 使用 JwtTokenProvider 直接生成，绕过登录流程，用于测试过滤器链和权限校验。
     *
     * @param userId   用户 ID
     * @param username 用户名
     * @param role     角色（SUPER_ADMIN / PLATFORM_ADMIN / SECURITY_ADMIN / SCOPE_ADMIN / READ_ONLY）
     * @return JWT token 字符串
     */
    protected String generateToken(String userId, String username, String role) {
        return jwtTokenProvider.generateToken(userId, username, role);
    }

    /** 生成 SUPER_ADMIN 角色 token（使用种子管理员信息） */
    protected String superAdminToken() {
        return generateToken(SEED_ADMIN_USER_ID, SEED_ADMIN_USERNAME, "SUPER_ADMIN");
    }

    /** 生成 PLATFORM_ADMIN 角色 token */
    protected String platformAdminToken() {
        return generateToken("user_platform_admin", "platformadmin", "PLATFORM_ADMIN");
    }

    /** 生成 SECURITY_ADMIN 角色 token */
    protected String securityAdminToken() {
        return generateToken("user_security_admin", "securityadmin", "SECURITY_ADMIN");
    }

    /** 生成 SCOPE_ADMIN 角色 token */
    protected String scopeAdminToken() {
        return generateToken("user_scope_admin", "scopeadmin", "SCOPE_ADMIN");
    }

    /** 生成 READ_ONLY 角色 token */
    protected String readOnlyToken() {
        return generateToken("user_read_only", "readonly", "READ_ONLY");
    }

    // ==================== HTTP 请求辅助 ====================

    /** 构建 Bearer Authorization 请求头 */
    protected HttpHeaders authHeaders(String token) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        if (token != null && !token.isEmpty()) {
            headers.set("Authorization", "Bearer " + token);
        }
        return headers;
    }

    /** 构建 Bearer Authorization + X-User-Id/X-User-Role 请求头（用于需要 header 的控制器） */
    protected HttpHeaders authHeadersWithUserContext(String token, String userId, String role) {
        HttpHeaders headers = authHeaders(token);
        headers.set("X-User-Id", userId);
        headers.set("X-User-Role", role);
        return headers;
    }

    /** GET 请求（带认证） */
    protected ResponseEntity<String> getWithToken(String url, String token) {
        return restTemplate.exchange(url, HttpMethod.GET, new HttpEntity<>(authHeaders(token)), String.class);
    }

    /** GET 请求（无认证） */
    protected ResponseEntity<String> getWithoutAuth(String url) {
        return restTemplate.exchange(url, HttpMethod.GET, new HttpEntity<>(new HttpHeaders()), String.class);
    }

    /** POST 请求（带认证 + 请求体） */
    protected ResponseEntity<String> postWithToken(String url, Object body, String token) {
        return restTemplate.exchange(url, HttpMethod.POST, new HttpEntity<>(body, authHeaders(token)), String.class);
    }

    /** POST 请求（无认证 + 请求体） */
    protected ResponseEntity<String> postWithoutAuth(String url, Object body) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        return restTemplate.exchange(url, HttpMethod.POST, new HttpEntity<>(body, headers), String.class);
    }

    /** PUT 请求（带认证 + 请求体） */
    protected ResponseEntity<String> putWithToken(String url, Object body, String token) {
        return restTemplate.exchange(url, HttpMethod.PUT, new HttpEntity<>(body, authHeaders(token)), String.class);
    }

    /** DELETE 请求（带认证） */
    protected ResponseEntity<String> deleteWithToken(String url, String token) {
        return restTemplate.exchange(url, HttpMethod.DELETE, new HttpEntity<>(authHeaders(token)), String.class);
    }

    /** DELETE 请求（带认证 + 请求体） */
    protected ResponseEntity<String> deleteWithTokenAndBody(String url, Object body, String token) {
        return restTemplate.exchange(url, HttpMethod.DELETE, new HttpEntity<>(body, authHeaders(token)), String.class);
    }

    // ==================== 响应解析辅助 ====================

    /** 从 JSON 响应中提取 code 字段值 */
    protected int extractCode(String json) throws Exception {
        JsonNode node = objectMapper.readTree(json);
        JsonNode codeNode = node.get("code");
        return codeNode != null ? codeNode.asInt() : -999;
    }

    /** 从 JSON 响应中提取 message 字段值 */
    protected String extractMessage(String json) throws Exception {
        JsonNode node = objectMapper.readTree(json);
        JsonNode msgNode = node.get("message");
        return msgNode != null ? msgNode.asText() : null;
    }

    /** 从 JSON 响应中提取 data 字段（作为 JsonNode） */
    protected JsonNode extractData(String json) throws Exception {
        return objectMapper.readTree(json).get("data");
    }

    /** 从 JSON 响应中提取 data.token 字段（登录响应） */
    protected String extractToken(String json) throws Exception {
        return extractData(json).get("token").asText();
    }

    /** 判断响应是否成功（code == 0） */
    protected boolean isSuccess(String json) throws Exception {
        return extractCode(json) == 0;
    }
}
