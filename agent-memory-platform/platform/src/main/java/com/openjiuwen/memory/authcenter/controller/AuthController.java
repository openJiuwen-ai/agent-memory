package com.openjiuwen.memory.authcenter.controller;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.authcenter.domain.User;
import com.openjiuwen.memory.authcenter.dto.ChangePasswordRequest;
import com.openjiuwen.memory.authcenter.dto.CreateUserRequest;
import com.openjiuwen.memory.authcenter.dto.UserVO;
import com.openjiuwen.memory.authcenter.service.RolePermissionService;
import com.openjiuwen.memory.authcenter.service.UserService;
import com.openjiuwen.memory.common.CommonResult;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.bind.annotation.*;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * 认证与用户管理 REST API
 */
@RestController
@RequestMapping("/api/v1")
public class AuthController {
    
    @Autowired
    private UserService userService;
    
    @Autowired
    private RolePermissionService rolePermissionService;
    
    @Autowired
    private com.openjiuwen.memory.authcenter.config.JwtTokenProvider jwtTokenProvider;
    
    /**
     * 用户登录
     */
    @PostMapping("/auth/login")
    public CommonResult<LoginResponse> login(@RequestBody LoginRequest request) {
        User user = userService.getByUsername(request.getUsername());
        if (user == null) {
            return CommonResult.error("用户名或密码错误");
        }
        
        // 验证密码
        if (!com.openjiuwen.memory.common.util.PasswordEncoder.matches(
                request.getPassword(), user.getPassword())) {
            return CommonResult.error("用户名或密码错误");
        }
        
        // 生成 JWT Token（30分钟有效期）
        String token = jwtTokenProvider.generateToken(user.getId(), user.getUsername(), user.getRole());

        // 不向登录响应透传密码哈希
        user.setPassword(null);

        LoginResponse response = new LoginResponse();
        response.setToken(token);
        response.setUser(UserVO.fromUser(user));

        return CommonResult.success(response);
    }
    
    /**
     * 获取当前用户信息（从 JWT 解析 userId 查 DB）。
     * 使用 UserVO 避免泄露 password 字段
     */
    @GetMapping("/auth/info")
    public CommonResult<UserVO> userInfo(jakarta.servlet.http.HttpServletRequest request) {
        String bearerToken = request.getHeader("Authorization");
        if (bearerToken == null || !bearerToken.startsWith("Bearer ")) {
            return CommonResult.error("未登录");
        }
        String token = bearerToken.substring(7);
        if (!jwtTokenProvider.validateToken(token)) {
            return CommonResult.error("令牌无效或已过期");
        }
        String userId = jwtTokenProvider.getUserIdFromToken(token);
        User user = userService.getById(userId);
        if (user == null) {
            return CommonResult.error("用户不存在");
        }
        // 使用 VO 对象，不返回 password 字段
        return CommonResult.success(UserVO.fromUser(user));
    }

    /**
     * 退出登录（JWT 无状态，前端清 token 即可；此端点用于审计记录）。
     */
    @PostMapping("/auth/logout")
    public CommonResult<Void> logout() {
        // JWT 无状态，服务端无需维护会话；前端清除本地 token 即完成登出
        return CommonResult.success(null);
    }
    
    /**
     * 获取用户列表
     * 使用 UserVO 避免泄露 password 字段
     */
    @GetMapping("/users")
    public CommonResult<List<UserVO>> listUsers() {
        List<User> users = userService.list();
        List<UserVO> userVOs = users.stream()
            .map(UserVO::fromUser)
            .collect(java.util.stream.Collectors.toList());
        return CommonResult.success(userVOs);
    }
    
    /**
     * 创建用户（带角色权限校验和Scope分配）
     */
    @PostMapping("/users")
    public CommonResult<User> createUser(
            @RequestHeader(value = "X-User-Role", required = false) String currentUserRole,
            @RequestHeader(value = "X-User-ScopeIds", required = false) String currentUserScopeIds,
            @RequestBody CreateUserRequest request) {
        try {
            // X-User-Role/X-User-ScopeIds 头未被任何后端过滤器从 JWT 注入（前端 users.ts 靠硬编码 SUPER_ADMIN 绕过）。
            // 头缺失时从 SecurityContext 解析当前操作者：role 取 JWT authorities（去掉 ROLE_ 前缀），
            // scopeIds 按当前用户名查库回填。否则 canCreateRole 收到 null 角色会一律拒绝创建。
            Authentication auth = SecurityContextHolder.getContext().getAuthentication();
            if ((currentUserRole == null || currentUserRole.isBlank())
                    && auth != null && auth.isAuthenticated() && !auth.getAuthorities().isEmpty()) {
                String authority = auth.getAuthorities().iterator().next().getAuthority();
                currentUserRole = authority.startsWith("ROLE_") ? authority.substring(5) : authority;
            }
            if (currentUserScopeIds == null && auth != null && auth.getName() != null) {
                User me = userService.getByUsername(auth.getName());
                if (me != null) {
                    currentUserScopeIds = me.getScopeIds();
                }
            }
            // 调用Service层的权限校验方法
            User user = userService.createUserWithPermissionCheck(currentUserRole, currentUserScopeIds, request);
            return CommonResult.success(user);
        } catch (Exception e) {
            return CommonResult.error(e.getMessage());
        }
    }
    
    /**
     * 管理员重置用户密码（不需要原密码）
     */
    @PutMapping("/users/{userId}/password")
    public CommonResult<Void> resetUserPassword(
            @PathVariable String userId,
            @RequestBody Map<String, String> request) {
        try {
            String newPassword = request.get("newPassword");
            if (newPassword == null || newPassword.isEmpty()) {
                return CommonResult.error("新密码不能为空");
            }
            
            userService.resetPasswordByAdmin(userId, newPassword);
            return CommonResult.success();
        } catch (Exception e) {
            return CommonResult.error(e.getMessage());
        }
    }
    
    /**
     * 用户修改自己的密码（需要验证原密码）
     */
    @PostMapping("/users/password/change")
    public CommonResult<Void> changeMyPassword(
            @RequestHeader("X-User-Id") String userId,
            @RequestBody ChangePasswordRequest request) {
        try {
            userService.changePassword(userId, request);
            return CommonResult.success();
        } catch (Exception e) {
            return CommonResult.error(e.getMessage());
        }
    }
    
    /**
     * 更新用户（支持scopeIds的JSON序列化）
     * 使用 UserVO 返回，避免泄露 password 字段
     */
    @PutMapping("/users/{userId}")
    public CommonResult<UserVO> updateUser(@PathVariable String userId, @RequestBody User user) {
        try {
            // 如果传入新密码，进行合法性校验并加密
            if (user.getPassword() != null && !user.getPassword().isEmpty()) {
                if (user.getPassword().length() < 6) {
                    return CommonResult.error("密码长度至少6位");
                }
                if (user.getPassword().length() > 20) {
                    return CommonResult.error("密码长度不能超过20位");
                }
                // BCrypt 加密
                user.setPassword(com.openjiuwen.memory.common.util.PasswordEncoder.encode(user.getPassword()));
            } else {
                // 没有传入新密码，保留旧密码
                User existingUser = userService.getById(userId);
                if (existingUser != null) {
                    user.setPassword(existingUser.getPassword());
                }
            }
                
            // 处理scopeIds的JSON序列化（如果scopeIds是List类型）
            if (user.getScopeIds() != null && !user.getScopeIds().isEmpty()) {
                ObjectMapper objectMapper = new ObjectMapper();
                try {
                    // 如果scopeIds已经是JSON字符串，尝试解析验证
                    objectMapper.readValue(user.getScopeIds(), new TypeReference<List<String>>(){});
                } catch (Exception e) {
                    // 如果不是JSON格式，将其转换为JSON字符串
                    // 这里假设传入的是逗号分隔的字符串，需要前端传递JSON格式
                    user.setScopeIds(user.getScopeIds());
                }
            }
                
            user.setId(userId);
            user.setUpdatedAt(LocalDateTime.now());
                
            boolean success = userService.updateById(user);
            if (success) {
                // 使用 VO 对象返回，不泄露 password
                return CommonResult.success(UserVO.fromUser(user));
            } else {
                return CommonResult.error("更新用户失败");
            }
        } catch (Exception e) {
            return CommonResult.error("更新用户失败：" + e.getMessage());
        }
    }
    
    /**
     * 删除用户
     */
    @DeleteMapping("/users/{userId}")
    public CommonResult<Void> deleteUser(@PathVariable String userId) {
        // 保护默认admin用户不被删除
        if ("user_admin".equals(userId)) {
            return CommonResult.error("默认管理员用户不允许删除");
        }
        
        // 保护SUPER_ADMIN角色用户不被删除
        User user = userService.getById(userId);
        if (user != null && "SUPER_ADMIN".equals(user.getRole())) {
            return CommonResult.error("超级管理员用户不允许删除");
        }
        
        boolean success = userService.removeById(userId);
        if (success) {
            return CommonResult.success();
        } else {
            return CommonResult.error("删除用户失败");
        }
    }
    
    /**
     * 获取角色列表（从权限表去重）
     */
    @GetMapping("/roles")
    public CommonResult<List<String>> listRoles() {
        // 返回固定的5个角色
        List<String> roles = List.of(
            "SUPER_ADMIN",
            "PLATFORM_ADMIN",
            "SECURITY_ADMIN",
            "SCOPE_ADMIN",
            "READ_ONLY"
        );
        return CommonResult.success(roles);
    }
    
    /**
     * 登录响应体
     */
    public static class LoginResponse {
        private String token;
        private UserVO user;
        
        public String getToken() {
            return token;
        }
        
        public void setToken(String token) {
            this.token = token;
        }
        
        public UserVO getUser() {
            return user;
        }
        
        public void setUser(UserVO user) {
            this.user = user;
        }
    }
    
    /**
     * 登录请求体
     */
    public static class LoginRequest {
        private String username;
        private String password;
        
        public String getUsername() {
            return username;
        }
        
        public void setUsername(String username) {
            this.username = username;
        }
        
        public String getPassword() {
            return password;
        }
        
        public void setPassword(String password) {
            this.password = password;
        }
    }
}
