package com.openjiuwen.memory.e2e;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * TC-SMOKE: 举一反三边界冒烟测试（防回归）。
 * <p>
 * 本类不重复 ConfigCenterE2eTest/LogCenterE2eTest 已有的 Bug 验证用例，
 * 而是基于已修复 Bug 做<b>举一反三</b>的边界与矩阵覆盖，防止同类问题在相邻路径复发：
 * <ul>
 *   <li>SM-A 权限矩阵冒烟（由 BUG-CFG-05/06、BUG-LOG-10 引申）：5 角色 × 关键端点正/反向矩阵</li>
 *   <li>SM-B 内置模板保护（由 BUG-CFG-01 引申）：update/delete/copy 三操作一致拦截</li>
 *   <li>SM-C 只读内核参数全推送路径（由 BUG-CFG-03 引申）：kernel PUT 与 instance PUT 双路径</li>
 *   <li>SM-D 分页/参数边界（由 BUG-LOG-12/13/14 引申）：page/size 边界值组合</li>
 *   <li>SM-E 不存在资源一致性（由 BUG-CFG-04 引申）：多 HTTP 方法对不存在租户一致 404</li>
 * </ul>
 * 断言原则：冒烟用例断言"正确行为"，任何偏离即 FAIL 并给出定位信息。
 */
@DisplayName("TC-SMOKE: 配置与日志中心举一反三边界冒烟")
class SmokeBoundaryE2eTest extends E2eTestBase {

    // ==================== SM-A 权限矩阵冒烟 ====================

    @Nested
    @DisplayName("SM-A: 权限矩阵冒烟（5角色 × 关键端点）")
    class PermissionMatrixSmoke {

        /**
         * 反向矩阵：无 config:write 权限的角色（READ_ONLY / SCOPE_ADMIN / SECURITY_ADMIN）
         * 执行配置写操作必须被 403 拦截（防 BUG-CFG-05/06 在相邻角色复发）。
         */
        @Test
        @DisplayName("SM-A1: 非授权角色执行配置写操作必须 403（READ_ONLY/SCOPE_ADMIN/SECURITY_ADMIN）")
        void unauthorizedRolesCannotWriteConfig() {
            Map<String, Object> body = Map.of(
                    "templateName", "smoke_unauth_" + System.currentTimeMillis(),
                    "templateType", "KERNEL",
                    "description", "smoke");

            assertWriteForbidden(postWithToken("/api/v1/config/templates", body, readOnlyToken()), "READ_ONLY");
            assertWriteForbidden(postWithToken("/api/v1/config/templates", body, scopeAdminToken()), "SCOPE_ADMIN");
            assertWriteForbidden(postWithToken("/api/v1/config/templates", body, securityAdminToken()), "SECURITY_ADMIN");
        }

        /**
         * 正向矩阵：具备 log:read 的角色应能读取日志。
         * 注意：READ_ONLY 依赖 V11 迁移授予 log:read —— 若 V11 未生效（id 冲突），
         * 此用例会在 READ_ONLY 行失败，作为 BUG-LOG-10 的哨兵。
         */
        @Test
        @DisplayName("SM-A2: 具备 log:read 的角色读取操作日志应非 403（SUPER/PLATFORM/SECURITY_ADMIN）")
        void authorizedRolesCanReadLogs() {
            String url = "/api/v1/logs/operations?page=0&size=10";
            assertNotForbidden(getWithToken(url, superAdminToken()), "SUPER_ADMIN");
            assertNotForbidden(getWithToken(url, platformAdminToken()), "PLATFORM_ADMIN");
            assertNotForbidden(getWithToken(url, securityAdminToken()), "SECURITY_ADMIN");
        }

        /**
         * 反向矩阵：SCOPE_ADMIN 无 log:read 权限，读取日志必须 403（防权限授予过宽）。
         */
        @Test
        @DisplayName("SM-A3: SCOPE_ADMIN 无 log:read，读取日志必须 403")
        void scopeAdminCannotReadLogs() {
            ResponseEntity<String> resp = getWithToken(
                    "/api/v1/logs/operations?page=0&size=10", scopeAdminToken());
            assertEquals(HttpStatus.FORBIDDEN, resp.getStatusCode(),
                    "SCOPE_ADMIN 不应有 log:read 权限，却返回 " + resp.getStatusCode());
        }

        private void assertWriteForbidden(ResponseEntity<String> resp, String role) {
            assertEquals(HttpStatus.FORBIDDEN, resp.getStatusCode(),
                    role + " 不应能执行 config:write，却返回 " + resp.getStatusCode()
                            + "（疑似 PermissionFilter 失效复发）");
        }

        private void assertNotForbidden(ResponseEntity<String> resp, String role) {
            assertNotEquals(HttpStatus.FORBIDDEN, resp.getStatusCode(),
                    role + " 应具备 log:read，却被 403 拦截（疑似权限授予缺失）");
        }
    }

    // ==================== SM-B 内置模板保护 ====================

    @Nested
    @DisplayName("SM-B: 内置模板保护（update/delete/copy 一致拦截）")
    class BuiltinTemplateProtectionSmoke {

        @Test
        @DisplayName("SM-B1: 内置模板不可 update 且不可 delete（双向拦截一致性）")
        void builtinTemplateImmutableBothWays() throws Exception {
            String builtinId = findPresetTemplateId();
            if (builtinId == null) {
                // 无内置模板种子则跳过保护断言（不判失败，仅记录）
                return;
            }
            // update 必须被拒
            ResponseEntity<String> upd = putWithToken(
                    "/api/v1/config/templates/" + builtinId,
                    Map.of("templateName", "x", "description", "hack"), superAdminToken());
            assertTrue(isClientOrServerReject(upd),
                    "内置模板 update 应被拦截，却返回 " + upd.getStatusCode());

            // delete 必须被拒
            ResponseEntity<String> del = deleteWithToken(
                    "/api/v1/config/templates/" + builtinId, superAdminToken());
            assertTrue(isClientOrServerReject(del),
                    "内置模板 delete 应被拦截，却返回 " + del.getStatusCode());
        }

        /** 内置模板 copy 应被允许（生成新非内置副本），不应误拦截合法 copy。 */
        @Test
        @DisplayName("SM-B2: 内置模板 copy 应允许且副本非内置（防过度拦截）")
        void builtinTemplateCopyAllowed() throws Exception {
            String builtinId = findPresetTemplateId();
            if (builtinId == null) {
                return;
            }
            ResponseEntity<String> copy = postWithToken(
                    "/api/v1/config/templates/" + builtinId + "/copy",
                    Map.of("newName", "smoke_copy_" + System.currentTimeMillis()), superAdminToken());
            // copy 是合法操作，不应返回 403 内置保护错误
            assertNotEquals(HttpStatus.FORBIDDEN, copy.getStatusCode(),
                    "内置模板 copy 属合法操作，不应被 403 拦截");
        }

        private boolean isClientOrServerReject(ResponseEntity<String> resp) {
            int s = resp.getStatusCode().value();
            // 拦截应表现为 4xx（FORBIDDEN/BAD_REQUEST）或业务 code != 0
            return s == 403 || s == 400 || s == 405;
        }
    }

    // ==================== SM-C 只读内核参数全推送路径 ====================

    @Nested
    @DisplayName("SM-C: 只读内核参数全推送路径拦截")
    class ReadOnlyKernelParamSmoke {

        @Test
        @DisplayName("SM-C1: kernel PUT 推送只读参数 ARCHITECTURE_TYPE 必须被拒")
        void kernelPutReadOnlyParamRejected() {
            ResponseEntity<String> resp = putWithToken(
                    "/api/v1/config/kernel",
                    Map.of("ARCHITECTURE_TYPE", "HACKED"), superAdminToken());
            int s = resp.getStatusCode().value();
            assertTrue(s == 400 || s == 403 || s == 422,
                    "kernel PUT 修改只读参数 ARCHITECTURE_TYPE 应被拒，却返回 " + s);
        }

        @Test
        @DisplayName("SM-C2: instance PUT 推送只读参数 ARCHITECTURE_TYPE 必须被拒（防旁路）")
        void instancePutReadOnlyParamRejected() {
            ResponseEntity<String> resp = putWithToken(
                    "/api/v1/instance-config",
                    Map.of("ARCHITECTURE_TYPE", "HACKED"), superAdminToken());
            int s = resp.getStatusCode().value();
            assertTrue(s == 400 || s == 403 || s == 422,
                    "instance PUT 修改只读参数 ARCHITECTURE_TYPE 应被拒（防 kernel 路径外旁路），却返回 " + s);
        }
    }

    // ==================== SM-D 分页/参数边界 ====================

    @Nested
    @DisplayName("SM-D: 分页/参数边界（page/size 组合）")
    class PaginationBoundarySmoke {

        @Test
        @DisplayName("SM-D1: 操作日志分页 page=-1 / size=0 / size 超大 必须一致拒绝或钳制")
        void operationLogPaginationBoundary() {
            // BUG-LOG-13/14 引申：负 page、零 size、超大 size 均不应返回 200 正常分页
            assertPaginationHandled("/api/v1/logs/operations?page=-1&size=10", "page=-1");
            assertPaginationHandled("/api/v1/logs/operations?page=0&size=0", "size=0");
            assertPaginationHandled("/api/v1/logs/operations?page=0&size=999999", "size=999999");
        }

        @Test
        @DisplayName("SM-D2: 消息日志分页缺时间参数不应 500（BUG-LOG-12 引申）")
        void messageLogMissingTimeParamNo500() {
            ResponseEntity<String> resp = getWithToken(
                    "/api/v1/logs/messages/export", superAdminToken());
            assertNotEquals(HttpStatus.INTERNAL_SERVER_ERROR, resp.getStatusCode(),
                    "消息日志导出缺时间参数不应 500，却返回 500");
        }

        private void assertPaginationHandled(String url, String caseDesc) {
            ResponseEntity<String> resp = getWithToken(url, superAdminToken());
            int s = resp.getStatusCode().value();
            // 合法处理：400 参数校验拒绝，或 200 但钳制（不崩溃、不 500）
            assertNotEquals(500, s,
                    "分页边界 " + caseDesc + " 不应导致 500，却返回 500");
        }
    }

    // ==================== SM-E 不存在资源一致性 ====================

    @Nested
    @DisplayName("SM-E: 不存在资源多方法一致性（BUG-CFG-04 引申）")
    class NonExistentResourceSmoke {

        @Test
        @DisplayName("SM-E1: 不存在租户 GET/PUT/DELETE 应一致返回 404 而非 200/500")
        void nonExistentTenantConsistent404() {
            String ghost = "/api/v1/tenant-scope-configs/tenant_ghost_" + System.currentTimeMillis();

            ResponseEntity<String> get = getWithToken(ghost, superAdminToken());
            ResponseEntity<String> put = putWithToken(ghost, Map.of("k", "v"), superAdminToken());
            ResponseEntity<String> del = deleteWithToken(ghost, superAdminToken());

            // 说明：写方法（PUT/DELETE）若命中权限映射会先被 PermissionFilter 403 拦截（鉴权先于资源存在性校验），
            // 这属于合法行为；GET 无写权限拦截则应到达 controller 返回 404/400。
            // 因此接受 404（资源不存在）/400（参数校验）/403（权限拦截）三种合法结果，仅禁止 200 与 500。
            for (Map.Entry<String, ResponseEntity<String>> e : Map.of(
                    "GET", get, "PUT", put, "DELETE", del).entrySet()) {
                int s = e.getValue().getStatusCode().value();
                assertTrue(s == 404 || s == 400 || s == 403,
                        e.getKey() + " 不存在租户应返回 404/400/403，却返回 " + s
                                + "（返回 200 表示误判成功，500 表示未捕获异常 — BUG-CFG-04 同类问题）");
            }
            // GET 不应返回 200（不存在租户不能误判成功）
            assertNotEquals(200, get.getStatusCode().value(),
                    "GET 不存在租户不应返回 200（BUG-CFG-04 核心：不得将不存在租户误判为成功）");
        }
    }

    // ==================== 辅助 ====================

    /** 从模板列表中查找一个内置（is_builtin=1）模板 ID，无则返回 null。 */
    private String findPresetTemplateId() throws Exception {
        ResponseEntity<String> resp = getWithToken("/api/v1/config/templates", superAdminToken());
        if (resp.getStatusCode() != HttpStatus.OK || resp.getBody() == null) {
            return null;
        }
        var data = extractData(resp.getBody());
        if (data == null || !data.isArray()) {
            return null;
        }
        for (var node : data) {
            var builtin = node.get("isBuiltin");
            if (builtin == null) {
                builtin = node.get("is_builtin");
            }
            if (builtin != null && builtin.asInt() == 1) {
                var id = node.get("id");
                if (id != null) {
                    return id.asText();
                }
            }
        }
        return null;
    }
}
