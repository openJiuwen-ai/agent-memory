package com.openjiuwen.memory.common.client.mock;

import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.dto.AddMessagesRequest;
import com.openjiuwen.memory.common.client.dto.DeleteByScopeRequest;
import com.openjiuwen.memory.common.client.dto.DeleteVariablesRequest;
import com.openjiuwen.memory.common.client.dto.GetVariablesRequest;
import com.openjiuwen.memory.common.client.dto.GetUserMemByPageRequest;
import com.openjiuwen.memory.common.client.dto.MemVariable;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.client.dto.RawResponses;
import com.openjiuwen.memory.common.client.dto.SearchHistorySummaryRequest;
import com.openjiuwen.memory.common.client.dto.SearchMemoryRequest;
import com.openjiuwen.memory.common.client.dto.UpdateMemoryRequest;
import com.openjiuwen.memory.common.client.dto.UpdateVariablesRequest;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

/**
 * <b>【可删除】仅供演示的 Mock 实现</b>——仅在 {@code platform.memory-service.mode=mock} 时生效。
 * <p>
 * 隔离约定：
 * <ul>
 *   <li>本类是 {@link MemoryEngineClient} 的备选实现，业务代码（Service/Controller）只依赖接口，
 *       绝不 import 本类——删掉本文件不影响真实版本。</li>
 *   <li>内置内存状态（{@link ConcurrentHashMap}），不调任何 :8516、不碰真实库。</li>
 *   <li>响应结构严格对齐真实 :8516（10 已有端点按实测）+ 《记忆服务API能力清单》契约（14 缺口 + 3 admin），
 *       故切回真实模式（mode=real/sqlite/mysql/gaussdb）时前端无感。</li>
 * </ul>
 * <p>
 * <b>删除清单</b>（去 mock 时）：①本文件 + mock 包；②application-mock.yml；
 * ③WebClientConfig 上的 \@ConditionalOnProperty（可选，留也无害，matchIfMissing 默认真实生效）。
 */
@Component
@ConditionalOnProperty(prefix = "platform.memory-service", name = "mode", havingValue = "mock")
public class MockMemoryEngineClient implements MemoryEngineClient {

    private static final String DEFAULT = "__default__";

    private final Map<String, MockMem> store = new ConcurrentHashMap<>();
    private final Map<String, Map<String, String>> variables = new ConcurrentHashMap<>();

    public MockMemoryEngineClient() {
        // 种子数据，对齐真实 :8516 __default__ 的样貌
        seed(DEFAULT, DEFAULT, "用户发送了默认测试消息。", "summary");
        seed(DEFAULT, DEFAULT, "用户发送了默认消息。", "summary");
        seed(DEFAULT, DEFAULT, "用户喜欢蓝色。", "user_profile");
    }

    // ===== 记忆列表 / 检索 =====

    @Override
    public PageResult<MemoryItem> getUserMemByPage(GetUserMemByPageRequest req) {
        String u = norm(req.getUserId()), s = norm(req.getScopeId());
        int pageSize = req.getPageSize() == null ? 10 : req.getPageSize();
        int pageIdx = req.getPageIdx() == null ? 1 : req.getPageIdx();
        String type = req.getMemoryType() == null || req.getMemoryType().isBlank() || "unknown".equalsIgnoreCase(req.getMemoryType())
                ? null : req.getMemoryType().toLowerCase();
        List<MemoryItem> filtered = store.values().stream()
                .filter(m -> m.userId.equals(u) && m.scopeId.equals(s))
                .filter(m -> type == null || m.type.equals(type))
                .map(m -> item(m, null))
                .toList();
        int total = filtered.size();
        int from = Math.max(0, (pageIdx - 1) * pageSize);
        int to = Math.min(total, from + pageSize);
        List<MemoryItem> page = from < to ? filtered.subList(from, to) : List.of();
        return PageResult.of(page, total, pageIdx, pageSize);
    }

    @Override
    public List<MemoryItem> searchMemory(SearchMemoryRequest req) {
        String u = norm(req.getUserId()), s = norm(req.getScopeId());
        int num = req.getNum() == null ? 10 : req.getNum();
        String q = req.getQuery() == null ? "" : req.getQuery();
        return store.values().stream()
                .filter(m -> m.userId.equals(u) && m.scopeId.equals(s))
                .map(m -> item(m, m.content.contains(q) ? 0.92 : 0.30))
                .sorted((a, b) -> Double.compare(b.getScore(), a.getScore()))
                .limit(num)
                .toList();
    }

    @Override
    public List<MemoryItem> searchHistorySummary(SearchHistorySummaryRequest req) {
        return searchMemory(toSearchReq(req)).stream()
                .filter(m -> "summary".equals(m.getType()))
                .toList();
    }

    // ===== 记忆写入 / 修改 =====

    @Override
    public RawResponses.StatusMessage addMessages(AddMessagesRequest req) {
        String u = norm(req.getUserId()), s = norm(req.getScopeId());
        int n = req.getMessages() == null ? 0 : req.getMessages().size();
        for (int i = 0; i < n; i++) {
            String content = String.valueOf(req.getMessages().get(i).getOrDefault("content", ""));
            String type = Boolean.TRUE.equals(req.getEnableUserProfile()) && i == 0 ? "user_profile" : "summary";
            seed(u, s, content.isBlank() ? "（空消息）" : content, type);
        }
        return status("Messages added successfully (mock, " + n + " items)");
    }

    @Override
    public RawResponses.StatusMessage updateMemById(UpdateMemoryRequest req) {
        MockMem m = store.get(req.getMemId());
        if (m != null) {
            m.content = req.getMemory();
        }
        return status("Memory " + req.getMemId() + " updated successfully (mock)");
    }

    // ===== 记忆删除（含 :8516 缺口端点的 mock 实现） =====

    @Override
    public RawResponses.DeleteResult deleteMemByScope(DeleteByScopeRequest req) {
        String s = norm(req.getScopeId());
        int n = (int) store.values().stream().filter(m -> m.scopeId.equals(s)).peek(m -> store.remove(m.memId)).count();
        return delete(n);
    }

    @Override
    public RawResponses.DeleteResult deleteMemById(String memId, String userId, String scopeId) {
        boolean removed = store.remove(memId) != null;
        return delete(removed ? 1 : 0);
    }

    @Override
    public RawResponses.DeleteResult deleteMemByUserId(String userId, String scopeId) {
        String u = norm(userId), s = norm(scopeId);
        int n = (int) store.values().stream().filter(m -> m.userId.equals(u) && m.scopeId.equals(s))
                .peek(m -> store.remove(m.memId)).count();
        return delete(n);
    }

    @Override
    public RawResponses.DeleteResult batchDeleteMem(List<String> memIds, String userId, String scopeId) {
        int n = 0;
        for (String id : memIds == null ? List.<String>of() : memIds) {
            if (store.remove(id) != null) n++;
        }
        return delete(n);
    }

    // ===== 变量 =====

    @Override
    public Map<String, String> getVariables(GetVariablesRequest req) {
        Map<String, String> all = variables.getOrDefault(varKey(req.getUserId(), req.getScopeId()), Map.of());
        if (req.getNames() == null || req.getNames().isEmpty()) return all;
        return all.entrySet().stream().filter(e -> req.getNames().contains(e.getKey()))
                .collect(Collectors.toMap(Map.Entry::getKey, Map.Entry::getValue));
    }

    @Override
    public RawResponses.StatusMessage updateVariables(UpdateVariablesRequest req) {
        variables.computeIfAbsent(varKey(req.getUserId(), req.getScopeId()), k -> new ConcurrentHashMap<>())
                .putAll(req.getVariables() == null ? Map.of() : req.getVariables());
        return status("Variables updated successfully (mock)");
    }

    @Override
    public RawResponses.DeleteResult deleteVariables(DeleteVariablesRequest req) {
        Map<String, String> m = variables.get(varKey(req.getUserId(), req.getScopeId()));
        int n = 0;
        if (m != null && req.getNames() != null) {
            for (String name : req.getNames()) if (m.remove(name) != null) n++;
        }
        return delete(n);
    }

    // ===== 健康 =====

    @Override
    public RawResponses.Health health() {
        RawResponses.Health h = new RawResponses.Health();
        h.setStatus("healthy");
        h.setMessage("Memory Engine API is running (mock)");
        return h;
    }

    // ===== Admin / Config / Dreaming / Scope 发现/ 迁移（:8516 缺口，mock 占位） =====

    @Override
    public List<String> listScopes(String adminUserId) {
        // 返回 store 中所有不同的 scopeId（排除 __default__ 以避免重复）
        return store.values().stream()
                .map(m -> m.scopeId)
                .distinct()
                .collect(Collectors.toList());
    }

    // ===== Admin / Config / Dreaming / 迁移（:8516 缺口，mock 占位） =====

    @Override
    public Object restartKernel() { return Map.of("status", "success", "message", "kernel restarted (mock)"); }

    @Override
    public Object reloadConfig() { return Map.of("status", "success", "message", "config reloaded (mock)"); }

    @Override
    public Object clearCache() { return Map.of("status", "success", "message", "cache cleared (mock)"); }

    @Override
    public Object rebuildIndex() { return Map.of("status", "success", "message", "index rebuilt (mock)"); }

    @Override
    public Object startDreaming(Object config) { return Map.of("started", true, "message", "dreaming started (mock)"); }

    @Override
    public Object stopDreaming(String scopeId, String userId) { return Map.of("stopped", true, "message", "dreaming stopped (mock)"); }

    @Override
    public Object dreamingStatus() {
        return Map.of("active_orchestrators", List.of(), "total_count", 0);
    }

    @Override
    public Object migrate(Map<String, Object> source, Map<String, Object> target) {
        return Map.of("task_id", "mock-migrate-" + UUID.randomUUID().toString().substring(0, 8),
                "status", "completed", "scope_count", store.size(), "duration_seconds", 1);
    }

    // ===== 用户消息日志（V3 §6.6.4 KR-MSG-01~04，mock 实现） =====

    @Override
    public Map<String, Object> queryKernelMessages(Map<String, Object> filter) {
        String filterUserId = filter != null ? String.valueOf(filter.get("user_id")) : null;
        String filterScopeId = filter != null ? String.valueOf(filter.get("scope_id")) : null;
        int pageIdx = filter != null && filter.get("page_idx") instanceof Number n ? n.intValue() : 1;
        int pageSize = filter != null && filter.get("page_size") instanceof Number n2 ? n2.intValue() : 20;

        List<Map<String, Object>> items = store.values().stream()
                .filter(m -> filterUserId == null || "null".equals(filterUserId) || filterUserId.isBlank()
                        || m.userId.equals(norm(filterUserId)))
                .filter(m -> filterScopeId == null || "null".equals(filterScopeId) || filterScopeId.isBlank()
                        || m.scopeId.equals(norm(filterScopeId)))
                .map(m -> {
                    Map<String, Object> item = new LinkedHashMap<>();
                    item.put("message_id", m.memId);
                    item.put("user_id", m.userId);
                    item.put("scope_id", m.scopeId);
                    item.put("role", "user");
                    item.put("content", m.content);
                    item.put("timestamp", m.createdAt != null ? m.createdAt.toString() : Instant.now().toString());
                    return item;
                })
                .toList();

        int total = items.size();
        int from = Math.max(0, (pageIdx - 1) * pageSize);
        int to = Math.min(total, from + pageSize);
        List<Map<String, Object>> page = from < to ? items.subList(from, to) : List.of();

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("total", total);
        result.put("page_idx", pageIdx);
        result.put("page_size", pageSize);
        result.put("items", page);
        return result;
    }

    @Override
    public Map<String, Object> statsKernelMessages(String scopeId, String userId, String sessionId,
                                                    String startTime, String endTime) {
        long total = store.values().stream()
                .filter(m -> userId == null || userId.isBlank() || m.userId.equals(norm(userId)))
                .filter(m -> scopeId == null || scopeId.isBlank() || m.scopeId.equals(norm(scopeId)))
                .count();
        Map<String, Object> byRole = new LinkedHashMap<>();
        byRole.put("user", total);
        byRole.put("assistant", 0L);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("total", total);
        result.put("by_role", byRole);
        return result;
    }

    @Override
    public byte[] exportKernelMessages(String scopeId, String userId, String sessionId,
                                        String startTime, String endTime, int limit) {
        StringBuilder csv = new StringBuilder();
        csv.append("message_id,user_id,scope_id,role,content,timestamp\n");
        store.values().stream()
                .filter(m -> userId == null || userId.isBlank() || m.userId.equals(norm(userId)))
                .filter(m -> scopeId == null || scopeId.isBlank() || m.scopeId.equals(norm(scopeId)))
                .limit(limit > 0 ? limit : 20000)
                .forEach(m -> csv.append(m.memId).append(",")
                        .append(m.userId).append(",")
                        .append(m.scopeId).append(",")
                        .append("user,")
                        .append(m.content != null ? m.content.replace(",", ";") : "").append(",")
                        .append(m.createdAt != null ? m.createdAt.toString() : "").append("\n"));
        return csv.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8);
    }

    @Override
    public Map<String, Object> getKernelMessageDetail(String msgId) {
        MockMem m = store.get(msgId);
        if (m == null) {
            return null;
        }
        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message_id", m.memId);
        detail.put("user_id", m.userId);
        detail.put("scope_id", m.scopeId);
        detail.put("session_id", "default-session");
        detail.put("role", "user");
        detail.put("content", m.content);
        detail.put("timestamp", m.createdAt != null ? m.createdAt.toString() : Instant.now().toString());
        return detail;
    }

    // ===== 内部 =====

    private void seed(String u, String s, String content, String type) {
        MockMem m = new MockMem();
        m.memId = genUlid();
        m.userId = u; m.scopeId = s; m.content = content; m.type = type;
        m.createdAt = Instant.now();
        store.put(m.memId, m);
    }

    private static String genUlid() {
        return "01" + UUID.randomUUID().toString().replace("-", "").substring(0, 24);
    }

    private static String norm(String v) { return v == null || v.isBlank() ? DEFAULT : v; }

    private static String varKey(String userId, String scopeId) { return norm(userId) + "|" + norm(scopeId); }

    private static MemoryItem item(MockMem m, Double score) {
        MemoryItem it = new MemoryItem();
        it.setMemId(m.memId); it.setContent(m.content); it.setType(m.type);
        if (score != null) it.setScore(score);
        return it;
    }

    private static RawResponses.StatusMessage status(String msg) {
        RawResponses.StatusMessage s = new RawResponses.StatusMessage();
        s.setStatus("success"); s.setMessage(msg);
        return s;
    }

    private static RawResponses.DeleteResult delete(int n) {
        RawResponses.DeleteResult d = new RawResponses.DeleteResult();
        d.setStatus("success"); d.setDeleted(n);
        return d;
    }

    private static SearchMemoryRequest toSearchReq(SearchHistorySummaryRequest r) {
        SearchMemoryRequest s = new SearchMemoryRequest();
        s.setQuery(r.getQuery()); s.setNum(r.getNum()); s.setUserId(r.getUserId());
        s.setScopeId(r.getScopeId()); s.setThreshold(r.getThreshold());
        return s;
    }

    private static final class MockMem {
        String memId, userId, scopeId, content, type;
        Instant createdAt;
    }
}
