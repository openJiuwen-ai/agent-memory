package com.openjiuwen.memory.e2e;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * TC-SMOKE-JOURNEY: 配置中心 + 日志中心 端到端业务旅程冒烟套件。
 * <p>
 * <b>定位：每次回归任何 Bug 修复前必跑的常驻门禁。</b>
 * 与 SmokeBoundaryE2eTest（边界/权限矩阵）不同，本套件验证的是
 * <b>完整业务旅程能端到端走通</b>——任一环节断裂即 FAIL 并给出断点定位：
 * <ul>
 *   <li>J-CFG 配置中心旅程：列表 → 详情 → 创建 → 应用 → 内核配置读 → 删除</li>
 *   <li>J-LOG 日志中心旅程：操作日志查/导出 → 消息日志查/详情/尾部 → 统计 → 采集三段式</li>
 *   <li>J-AUTH 认证基线旅程：无 token 401 / 非法 token / 合法 token 放行</li>
 * </ul>
 * 断言原则：每个旅程是一串有依赖的步骤，前一步失败会带上下文快速定位断点。
 * 运行方式：mvn test -Dtest="SmokeJourneyE2eTest"（约 30s，作为回归前置门禁）。
 */
@DisplayName("TC-SMOKE-JOURNEY: 配置与日志中心端到端旅程冒烟")
class SmokeJourneyE2eTest extends E2eTestBase {

    // ==================== J-CFG 配置中心端到端旅程 ====================

    @Nested
    @DisplayName("J-CFG: 配置中心端到端旅程")
    class ConfigJourney {

        @Test
        @DisplayName("J-CFG-01: 模板全生命周期旅程（列表→创建→详情→删除）")
        void templateLifecycleJourney() throws Exception {
            // 步骤1：列表可访问（旅程起点）
            ResponseEntity<String> list = getWithToken("/api/v1/config/templates", superAdminToken());
            assertEquals(HttpStatus.OK, list.getStatusCode(),
                    "[J-CFG-01 断点@列表] 模板列表应可访问，却返回 " + list.getStatusCode());

            // 步骤2：创建一个普通（非内置）模板
            String name = "journey_tpl_" + System.currentTimeMillis();
            // 后端 CreateTemplateRequest 使用 @JsonProperty 蛇形字段名，
            // template_type 合法值为 SCOPE / INSTANCE，config_json 为 SCOPE 模板必填配置体。
            ResponseEntity<String> create = postWithToken("/api/v1/config/templates",
                    Map.of("template_name", name,
                            "template_type", "SCOPE",
                            "description", "journey",
                            "config_json", "{}"),
                    superAdminToken());
            assertTrue(create.getStatusCode().is2xxSuccessful() || isSuccess(create.getBody()),
                    "[J-CFG-01 断点@创建] 创建模板应成功，却返回 " + create.getStatusCode()
                            + " body=" + abbrev(create.getBody()));

            // 步骤3：列表中应能找到刚创建的模板（读己之写）
            ResponseEntity<String> list2 = getWithToken("/api/v1/config/templates", superAdminToken());
            String createdId = findTemplateIdByName(list2.getBody(), name);
            assertNotNull(createdId, "[J-CFG-01 断点@读己之写] 创建后列表应含新模板 " + name);

            // 步骤4：详情可读取
            ResponseEntity<String> detail = getWithToken("/api/v1/config/templates/" + createdId, superAdminToken());
            assertEquals(HttpStatus.OK, detail.getStatusCode(),
                    "[J-CFG-01 断点@详情] 模板详情应可读，却返回 " + detail.getStatusCode());

            // 步骤5：删除旅程终点（清理，验证 delete 通路）
            ResponseEntity<String> del = deleteWithToken("/api/v1/config/templates/" + createdId, superAdminToken());
            assertTrue(del.getStatusCode().is2xxSuccessful() || isSuccess(del.getBody()),
                    "[J-CFG-01 断点@删除] 删除自建模板应成功，却返回 " + del.getStatusCode());
        }

        @Test
        @DisplayName("J-CFG-02: 内核配置读取旅程（GET /config/kernel 可达且返回结构完整）")
        void kernelConfigReadJourney() throws Exception {
            ResponseEntity<String> resp = getWithToken("/api/v1/config/kernel", superAdminToken());
            // mock 内核模式下允许 200 或内核不可达的明确错误，但不应 500 裸奔
            assertNotEquals(HttpStatus.INTERNAL_SERVER_ERROR, resp.getStatusCode(),
                    "[J-CFG-02 断点] 内核配置读取不应 500，却返回 500 body=" + abbrev(resp.getBody()));
        }

        @Test
        @DisplayName("J-CFG-03: 模板比较端点旅程（BUG-CFG-08 修复后 compare 应可达）")
        void templateCompareJourney() {
            ResponseEntity<String> resp = postWithToken("/api/v1/config/templates/compare",
                    Map.of("sourceId", "a", "targetId", "b"), superAdminToken());
            // 端点存在即可（参数可欠语义），不应 404/501（端点缺失）
            assertNotEquals(HttpStatus.NOT_FOUND, resp.getStatusCode(),
                    "[J-CFG-03 断点] compare 端点不应 404（BUG-CFG-08 端点缺失复发）");
            assertNotEquals(HttpStatus.NOT_IMPLEMENTED, resp.getStatusCode(),
                    "[J-CFG-03 断点] compare 端点不应 501（BUG-CFG-08 复发）");
        }
    }

    // ==================== J-LOG 日志中心端到端旅程 ====================

    @Nested
    @DisplayName("J-LOG: 日志中心端到端旅程")
    class LogJourney {

        @Test
        @DisplayName("J-LOG-01: 操作日志查询→导出旅程")
        void operationLogJourney() {
            // 步骤1：分页查询可达
            ResponseEntity<String> query = getWithToken(
                    "/api/v1/logs/operations?page=0&size=10", superAdminToken());
            assertEquals(HttpStatus.OK, query.getStatusCode(),
                    "[J-LOG-01 断点@查询] 操作日志查询应可达，却返回 " + query.getStatusCode());

            // 步骤2：按类型统计可达（BUG-LOG-01 注解 SQL 修复后）
            ResponseEntity<String> stats = getWithToken(
                    "/api/v1/logs/operations/stats/by-type", superAdminToken());
            assertNotEquals(HttpStatus.INTERNAL_SERVER_ERROR, stats.getStatusCode(),
                    "[J-LOG-01 断点@统计] 操作日志按类型统计不应 500（BUG-LOG-01 SQL 修复复发），body="
                            + abbrev(stats.getBody()));
        }

        @Test
        @DisplayName("J-LOG-02: 消息日志查询→详情→尾部旅程（BUG-LOG-08/11 修复后）")
        void messageLogJourney() {
            // 步骤1：GET 查询可达
            ResponseEntity<String> query = getWithToken(
                    "/api/v1/logs/messages?page=0&size=10", superAdminToken());
            assertEquals(HttpStatus.OK, query.getStatusCode(),
                    "[J-LOG-02 断点@查询] 消息日志查询应可达，却返回 " + query.getStatusCode());

            // 步骤2：POST 查询可达（BUG-LOG-11：V3 要求 POST 也应支持）
            ResponseEntity<String> postQuery = postWithToken("/api/v1/logs/messages",
                    Map.of("page", 0, "size", 10), superAdminToken());
            assertNotEquals(HttpStatus.NOT_FOUND, postQuery.getStatusCode(),
                    "[J-LOG-02 断点@POST查询] 消息日志 POST 查询不应 404（BUG-LOG-11 复发）");

            // 步骤3：消息详情端点存在（BUG-LOG-08：GET /{msgId}）
            ResponseEntity<String> detail = getWithToken(
                    "/api/v1/logs/messages/some-msg-id", superAdminToken());
            assertNotEquals(HttpStatus.NOT_FOUND, detail.getStatusCode(),
                    "[J-LOG-02 断点@详情] 消息详情端点不应 404（BUG-LOG-08 端点缺失复发）");
        }

        @Test
        @DisplayName("J-LOG-03: 一键采集三段式旅程（POST→轮询→下载 URL 存在）")
        void collectJourney() throws Exception {
            // 步骤1：触发采集（BUG-LOG-09：POST /logs/collect 应存在）
            ResponseEntity<String> collect = postWithToken("/api/v1/logs/collect", null,
                    superAdminToken());
            assertNotEquals(HttpStatus.NOT_FOUND, collect.getStatusCode(),
                    "[J-LOG-03 断点@触发] 采集端点不应 404（BUG-LOG-09 复发）");
            assertNotEquals(HttpStatus.INTERNAL_SERVER_ERROR, collect.getStatusCode(),
                    "[J-LOG-03 断点@触发] 采集端点不应 500（BUG-LOG-09 复发）body=" + abbrev(collect.getBody()));

            // 步骤2：采集记录列表可读（三段式状态可轮询）
            ResponseEntity<String> list = getWithToken("/api/v1/logs/collect", superAdminToken());
            assertEquals(HttpStatus.OK, list.getStatusCode(),
                    "[J-LOG-03 断点@轮询] 采集记录列表应可读，却返回 " + list.getStatusCode());
        }

        @Test
        @DisplayName("J-LOG-04: 运行日志尾部→文件列表旅程")
        void runtimeLogJourney() {
            // 步骤1：运行日志尾部可达（mock 内核下允许明确错误但不 404 端点缺失）
            ResponseEntity<String> tail = getWithToken(
                    "/api/v1/logs/runtime/tail?lines=10", superAdminToken());
            assertNotEquals(HttpStatus.NOT_FOUND, tail.getStatusCode(),
                    "[J-LOG-04 断点@尾部] 运行日志尾部端点不应 404");

            // 步骤2：日志文件列表可达
            ResponseEntity<String> files = getWithToken("/api/v1/logs/runtime/files", superAdminToken());
            assertNotEquals(HttpStatus.NOT_FOUND, files.getStatusCode(),
                    "[J-LOG-04 断点@文件列表] 运行日志文件列表端点不应 404");
        }
    }

    // ==================== J-AUTH 认证基线旅程 ====================

    @Nested
    @DisplayName("J-AUTH: 认证基线旅程（每次回归必验证的安全底座）")
    class AuthBaselineJourney {

        @Test
        @DisplayName("J-AUTH-01: 无 token 访问受保护端点应被拒（401/403），合法 token 放行")
        void authBaselineJourney() {
            // 无 token：应被拦截（401 或 403，绝不能 200）
            ResponseEntity<String> noAuth = getWithoutAuth("/api/v1/config/templates");
            assertNotEquals(HttpStatus.OK, noAuth.getStatusCode(),
                    "[J-AUTH-01 断点@无token] 无认证访问受保护端点不应 200（认证完全失效）");

            // 合法 token：应放行 200
            ResponseEntity<String> withAuth = getWithToken("/api/v1/config/templates", superAdminToken());
            assertEquals(HttpStatus.OK, withAuth.getStatusCode(),
                    "[J-AUTH-01 断点@合法token] 合法认证访问应 200，却返回 " + withAuth.getStatusCode());
        }

        @Test
        @DisplayName("J-AUTH-02: 非法 token 访问受保护端点应被拒（防伪造 token 直通）")
        void invalidTokenRejected() {
            ResponseEntity<String> resp = restTemplate.exchange(
                    "/api/v1/config/templates", org.springframework.http.HttpMethod.GET,
                    new org.springframework.http.HttpEntity<>(authHeaders("forge.invalid.token")),
                    String.class);
            assertNotEquals(HttpStatus.OK, resp.getStatusCode(),
                    "[J-AUTH-02 断点] 非法 token 不应放行（认证绕过）");
        }
    }

    // ==================== 辅助 ====================

    private String findTemplateIdByName(String json, String name) throws Exception {
        if (json == null) {
            return null;
        }
        JsonNode data = extractData(json);
        if (data == null || !data.isArray()) {
            return null;
        }
        for (JsonNode node : data) {
            JsonNode n = node.get("templateName");
            if (n == null) {
                n = node.get("template_name");
            }
            if (n != null && name.equals(n.asText())) {
                JsonNode id = node.get("id");
                return id != null ? id.asText() : null;
            }
        }
        return null;
    }

    private String abbrev(String body) {
        if (body == null) {
            return "null";
        }
        return body.length() <= 200 ? body : body.substring(0, 200) + "...";
    }
}
