package com.openjiuwen.memory.common.client;

import com.openjiuwen.memory.common.client.dto.DeleteByScopeRequest;
import com.openjiuwen.memory.common.client.dto.DeleteVariablesRequest;
import com.openjiuwen.memory.common.client.dto.GetVariablesRequest;
import com.openjiuwen.memory.common.client.dto.GetUserMemByPageRequest;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.client.dto.RawResponses;
import com.openjiuwen.memory.common.client.dto.SearchHistorySummaryRequest;
import com.openjiuwen.memory.common.client.dto.SearchMemoryRequest;
import com.openjiuwen.memory.common.client.dto.AddMessagesRequest;
import com.openjiuwen.memory.common.client.dto.UpdateMemoryRequest;
import com.openjiuwen.memory.common.client.dto.UpdateVariablesRequest;
import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;
import org.springframework.web.client.RestClient;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 默认实现：用 {@link RestClient}（同步）调用记忆服务 :8516。
 * <p>
 * 注意：不标 @Component，由 {@link com.openjiuwen.memory.opscenter.config.WebClientConfig#memoryEngineClient}
 * 以 @Bean 方式创建，以注入装配好的 RestClient（含 baseUrl / Bearer key / 超时）。
 * <p>
 * 实现说明（线上实测）：
 * <ul>
 *   <li>FIX-004A: getUserMemByPage 调用内核 /get_user_mem_by_page_with_total/ 端点，
 *       该端点返回的 total 是真实全局总数（非当前页条数）。PageResult.total 可直接用于计数。</li>
 *   <li>add_messages 仅返回 {status,message}，无 mem_id。</li>
 * </ul>
 */
public class DefaultMemoryEngineClient implements MemoryEngineClient {

    private final RestClient restClient;

    /** 流式下载专用：手动 createRequest 拿 ClientHttpResponse，完全自掌控流关闭时机（绕开 RestClient.exchange 自动关流）。
     *  FIX-007A: 类型从 SimpleClientHttpRequestFactory 改为 ClientHttpRequestFactory 接口，
     *  以兼容 HttpComponentsClientHttpRequestFactory（Apache HttpClient 5 连接池）。 */
    private final org.springframework.http.client.ClientHttpRequestFactory requestFactory;
    private final String baseUrl;
    private final String apiKey;

    public DefaultMemoryEngineClient(RestClient memoryRestClient) {
        this(memoryRestClient, null, null, null);
    }

    public DefaultMemoryEngineClient(RestClient memoryRestClient,
                                     org.springframework.http.client.ClientHttpRequestFactory requestFactory,
                                     String baseUrl, String apiKey) {
        this.restClient = memoryRestClient;
        this.requestFactory = requestFactory;
        this.baseUrl = baseUrl;
        this.apiKey = apiKey;
    }

    @Override
    public PageResult<MemoryItem> getUserMemByPage(GetUserMemByPageRequest req) {
        normalizeIds(req);
        RawResponses.GetMemByPageResponse body = post("/get_user_mem_by_page_with_total/", req,
                RawResponses.GetMemByPageResponse.class);
        List<MemoryItem> items = body.getResults() == null ? Collections.emptyList() : body.getResults();
        // FIX-004A: /get_user_mem_by_page_with_total/ 端点返回的 total 是真实全局总数。
        long total = body.getTotal() == null ? items.size() : body.getTotal();
        return PageResult.of(items, total,
                req.getPageIdx() == null ? 1 : req.getPageIdx(),
                req.getPageSize() == null ? 10 : req.getPageSize());
    }

    @Override
    public List<MemoryItem> searchMemory(SearchMemoryRequest req) {
        normalizeSearch(req);
        RawResponses.SearchResponse body = post("/search_memory/", req, RawResponses.SearchResponse.class);
        return body.getResults() == null ? Collections.emptyList() : body.getResults();
    }

    @Override
    public List<MemoryItem> searchHistorySummary(SearchHistorySummaryRequest req) {
        normalizeSummary(req);
        RawResponses.SearchResponse body = post("/search_user_history_summary/", req, RawResponses.SearchResponse.class);
        return body.getResults() == null ? Collections.emptyList() : body.getResults();
    }

    @Override
    public RawResponses.StatusMessage addMessages(AddMessagesRequest req) {
        return post("/add_messages/", req, RawResponses.StatusMessage.class);
    }

    @Override
    public RawResponses.StatusMessage updateMemById(UpdateMemoryRequest req) {
        return post("/update_mem_by_id/", req, RawResponses.StatusMessage.class);
    }

    @Override
    public RawResponses.DeleteResult deleteMemByScope(DeleteByScopeRequest req) {
        return post("/delete_mem_by_scope/", req, RawResponses.DeleteResult.class);
    }

    @Override
    public Map<String, String> getVariables(GetVariablesRequest req) {
        normalizeVars(req);
        RawResponses.GetVariablesResponse body = post("/get_variables/", req, RawResponses.GetVariablesResponse.class);
        return body.getVariables() == null ? Collections.emptyMap() : body.getVariables();
    }

    @Override
    public RawResponses.StatusMessage updateVariables(UpdateVariablesRequest req) {
        req.setUserId(norm(req.getUserId()));
        req.setScopeId(norm(req.getScopeId()));
        return post("/update_variables/", req, RawResponses.StatusMessage.class);
    }

    @Override
    public RawResponses.DeleteResult deleteVariables(DeleteVariablesRequest req) {
        req.setUserId(norm(req.getUserId()));
        req.setScopeId(norm(req.getScopeId()));
        return post("/delete_variables/", req, RawResponses.DeleteResult.class);
    }

    @Override
    public RawResponses.Health health() {
        return restClient.get().uri("/health").retrieve().body(RawResponses.Health.class);
    }

    @Override
    public RawResponses.GetMessageResponse getMessageById(String messageId) {
        return post("/get_message_by_id/", Map.of("message_id", messageId == null ? "" : messageId),
                RawResponses.GetMessageResponse.class);
    }

    @Override
    public RawResponses.DeleteResult deleteMemById(String memId, String userId, String scopeId) {
        return post("/delete_mem_by_id/",
                Map.of("mem_id", memId == null ? "" : memId,
                        "user_id", norm(userId),
                        "scope_id", norm(scopeId)),
                RawResponses.DeleteResult.class);
    }

    @Override
    public RawResponses.DeleteResult batchDeleteMem(List<String> memIds, String userId, String scopeId) {
        return post("/batch_delete_mem/",
                Map.of("mem_ids", memIds == null ? List.of() : memIds,
                        "user_id", norm(userId),
                        "scope_id", norm(scopeId)),
                RawResponses.DeleteResult.class);
    }

    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> getScopeConfig(String scopeId) {
        Map<String, Object> body = post("/get_scope_config",
                Map.of("scope_id", norm(scopeId)),
                Map.class);
        Object config = body == null ? null : body.get("config");
        return config instanceof Map ? (Map<String, Object>) config : null;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> setScopeConfig(String scopeId, Map<String, Object> config) {
        // 下发 Scope 配置前必须校验 scope_id 非空：
        // norm() 会把空值静默替换为 "__default__"，导致配置被下发到默认 scope 而非目标 scope，
        // 属于"配置错投"风险，必须在最后一道关卡拦截（而非依赖上游每个调用方都记得校验）。
        if (scopeId == null || scopeId.isBlank()) {
            throw new BizException(ResultCode.BAD_REQUEST,
                "下发 Scope 配置必须携带 scope_id：当前 scope_id 为空，无法确定下发目标。" +
                "请确认租户已绑定 scope（scope_registry.assigned_to_tenant_id）后再下发。");
        }
        Map<String, Object> body = post("/set_scope_config",
                Map.of("scope_id", scopeId, "config", config == null ? Collections.emptyMap() : config),
                Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    public boolean deleteScopeConfig(String scopeId) {
        Map<?, ?> body = post("/delete_scope_config",
                Map.of("scope_id", norm(scopeId)),
                Map.class);
        Object success = body == null ? null : body.get("success");
        return Boolean.TRUE.equals(success);
    }

    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> getKernelConfig() {
        Map<String, Object> body = restClient.get().uri("/admin/config")
                .retrieve()
                .body(Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> pushKernelConfig(Map<String, String> updates) {
        Map<String, Object> body = restClient.put().uri("/admin/config")
                .header("Content-Type", "application/json")
                .body(Map.of("updates", updates == null ? Collections.emptyMap() : updates))
                .retrieve()
                .body(Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Object reloadConfig() {
        Map<String, Object> body = post("/admin/reload-config", Collections.emptyMap(), Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Object restartKernel() {
        Map<String, Object> body = post("/admin/restart", Collections.emptyMap(), Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Object startDreaming(Object config) {
        Map<String, Object> body = post("/start_dreaming", config, Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Object stopDreaming(String scopeId, String userId) {
        Map<String, Object> req = new java.util.LinkedHashMap<>();
        req.put("scope_id", scopeId);
        req.put("user_id", userId);
        Map<String, Object> body = post("/stop_dreaming", req, Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Object dreamingStatus() {
        // GET 请求，不能用 post 辅助；:8516 返 {orchestrators: [...]}
        Map<String, Object> body = restClient.get().uri("/dreaming/status").retrieve().body(Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    // —— 内部工具 ——

    /** null/空 → "__default__"，避免 :8516 收到 null user_id/scope_id 报错 */
    private String norm(String s) {
        return (s == null || s.isBlank()) ? "__default__" : s;
    }

    private void normalizeIds(GetUserMemByPageRequest req) {
        req.setUserId(norm(req.getUserId()));
        req.setScopeId(norm(req.getScopeId()));
        // memory_type 为空时保留 DTO 默认 "unknown"，绝不传 null（:8516 的 null.lower() 会崩）
        if (req.getMemoryType() == null || req.getMemoryType().isBlank()) {
            req.setMemoryType(com.openjiuwen.memory.common.client.dto.MemoryType.UNKNOWN);
        }
    }

    private void normalizeSearch(SearchMemoryRequest req) {
        req.setUserId(norm(req.getUserId()));
        req.setScopeId(norm(req.getScopeId()));
        if (req.getNum() == null) req.setNum(10);
        if (req.getThreshold() == null) req.setThreshold(0.3);
    }

    private void normalizeSummary(SearchHistorySummaryRequest req) {
        req.setUserId(norm(req.getUserId()));
        req.setScopeId(norm(req.getScopeId()));
        if (req.getNum() == null) req.setNum(10);
        if (req.getThreshold() == null) req.setThreshold(0.3);
    }

    private void normalizeVars(GetVariablesRequest req) {
        req.setUserId(norm(req.getUserId()));
        req.setScopeId(norm(req.getScopeId()));
    }

    private <T> T post(String uri, Object body, Class<T> responseType) {
        return restClient.post().uri(uri)
                .header("Content-Type", "application/json")
                .body(body)
                .retrieve()
                .body(responseType);
    }

    // —— 运行日志 tail / download（内核 HTTP，不入库） ——

    @Override
    @SuppressWarnings("unchecked")
    public List<String> tailKernelLogs(int lines, String level, String eventType) {
        int safeLines = Math.max(1, Math.min(lines, 2000));
        // 内核 /logs/tail 返回 {lines: [...], total, file_size, last_modified}
        Map<String, Object> body = restClient.get().uri(uriBuilder -> {
            uriBuilder.path("/logs/tail")
                    .queryParam("lines", safeLines);
            if (level != null && !level.isBlank()) uriBuilder.queryParam("level", level);
            if (eventType != null && !eventType.isBlank()) uriBuilder.queryParam("event_type", eventType);
            return uriBuilder.build();
        }).retrieve().body(Map.class);
        if (body == null) return Collections.emptyList();
        Object linesObj = body.get("lines");
        if (linesObj instanceof List<?> list) {
            List<String> result = new java.util.ArrayList<>(list.size());
            for (Object o : list) result.add(o == null ? "" : o.toString());
            return result;
        }
        return Collections.emptyList();
    }

    /**
     * 流式下载内核日志：返回上游原始 ClientHttpResponse（含打开的响应体 InputStream）。
     * <p>
     * 关键生命周期：exchange 的 {@code closeResponse=false} 语义通过把 response 直接 return
     * 给调用方实现——RestClient 在回调返回 response 本身时不会提前关闭它，
     * 由最外层消费方（控制器 StreamingResponseBody / zip 打包）读取并 close。
     * 全链路不经过 byte[] 堆缓冲。
     */
    /**
     * 流式下载内核日志：手动用 {@link org.springframework.http.client.ClientHttpRequestFactory} 发 GET，
     * 返回仍打开的 {@link org.springframework.http.client.ClientHttpResponse}，由调用方 close（连带关 body 流）。
     *
     * <p>为何不用 RestClient.exchange 返回 response：exchange 回调返回后 RestClient
     * 会自动关闭底层流，调用方再 transferTo 读时抛 "stream is closed"（实测 bf6c1c6f 案例）。
     * 手动 createRequest 拿到的 response 生命周期完全归调用方，可安全流式读取。</p>
     * <p>FIX-007A: requestFactory 类型已从 SimpleClientHttpRequestFactory 改为 ClientHttpRequestFactory 接口，
     * 当前实现为 HttpComponentsClientHttpRequestFactory（Apache HttpClient 5 连接池）。</p>
     */
    @Override
    public org.springframework.http.client.ClientHttpResponse downloadKernelLogs(String filename) {
        if (requestFactory == null || baseUrl == null) {
            throw new IllegalStateException("downloadKernelLogs 需要注入 requestFactory/baseUrl（流式下载未配置）");
        }
        String base = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        java.net.URI uri;
        try {
            String encoded = java.net.URLEncoder.encode(filename, java.nio.charset.StandardCharsets.UTF_8);
            uri = new java.net.URI(base + "/logs/download?filename=" + encoded);
        } catch (java.net.URISyntaxException e) {
            throw new org.springframework.web.client.RestClientException("构造 /logs/download URI 失败: " + e.getMessage());
        }
        try {
            org.springframework.http.client.ClientHttpRequest request =
                    requestFactory.createRequest(uri, org.springframework.http.HttpMethod.GET);
            if (apiKey != null && !apiKey.isBlank()) {
                request.getHeaders().set("Authorization", "Bearer " + apiKey);
            }
            org.springframework.http.client.ClientHttpResponse resp = request.execute();
            if (!resp.getStatusCode().is2xxSuccessful()) {
                int code = resp.getStatusCode().value();
                resp.close();
                throw new org.springframework.web.client.RestClientException("内核 /logs/download 返回 " + code);
            }
            System.out.println("[MemoryEngineClient] downloadKernelLogs(stream) OK uri=" + uri
                    + " contentLength=" + resp.getHeaders().getContentLength());
            return resp;
        } catch (java.io.IOException e) {
            System.err.println("[MemoryEngineClient] downloadKernelLogs(stream) FAIL uri=" + uri
                    + " filename=" + filename + " error=" + e.getClass().getName() + ": " + e.getMessage());
            throw new org.springframework.web.client.RestClientException("内核 /logs/download 请求失败: " + e.getMessage(), e);
        }
    }

    // —— 用户消息日志 KR-MSG-01~04（V3 §6.6.4，数据源=内核 user_message 表）——

    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> queryKernelMessages(Map<String, Object> filter) {
        Map<String, Object> safe = filter == null ? Collections.emptyMap() : filter;
        Map<String, Object> body = post("/admin/messages/query", safe, Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> statsKernelMessages(String scopeId, String userId, String sessionId,
                                                   String startTime, String endTime) {
        Map<String, Object> body = restClient.get().uri(uriBuilder -> {
            uriBuilder.path("/admin/messages/stats");
            if (scopeId != null && !scopeId.isBlank()) uriBuilder.queryParam("scope_id", scopeId);
            if (userId != null && !userId.isBlank()) uriBuilder.queryParam("user_id", userId);
            if (sessionId != null && !sessionId.isBlank()) uriBuilder.queryParam("session_id", sessionId);
            if (startTime != null && !startTime.isBlank()) uriBuilder.queryParam("start_time", startTime);
            if (endTime != null && !endTime.isBlank()) uriBuilder.queryParam("end_time", endTime);
            return uriBuilder.build();
        }).retrieve().body(Map.class);
        return body == null ? Collections.emptyMap() : body;
    }

    @Override
    public byte[] exportKernelMessages(String scopeId, String userId, String sessionId,
                                       String startTime, String endTime, int limit) {
        int safeLimit = Math.max(1, Math.min(limit, 20000));
        byte[] bytes = restClient.get().uri(uriBuilder -> {
            uriBuilder.path("/admin/messages/export")
                    .queryParam("limit", safeLimit);
            if (scopeId != null && !scopeId.isBlank()) uriBuilder.queryParam("scope_id", scopeId);
            if (userId != null && !userId.isBlank()) uriBuilder.queryParam("user_id", userId);
            if (sessionId != null && !sessionId.isBlank()) uriBuilder.queryParam("session_id", sessionId);
            if (startTime != null && !startTime.isBlank()) uriBuilder.queryParam("start_time", startTime);
            if (endTime != null && !endTime.isBlank()) uriBuilder.queryParam("end_time", endTime);
            return uriBuilder.build();
        }).retrieve().body(byte[].class);
        return bytes == null ? new byte[0] : bytes;
    }

    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> getKernelMessageDetail(String msgId) {
        if (msgId == null || msgId.isBlank()) {
            return null;
        }
        try {
            return restClient.get().uri("/admin/messages/detail/" + msgId)
                    .retrieve().body(Map.class);
        } catch (Exception e) {
            // 内核 404（消息不存在）→ null，由上层决定如何响应
            return null;
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public List<Map<String, Object>> listKernelLogFiles() {
        // 内核 /logs/files 返回 {files: [...], total, log_dir}
        Map<String, Object> body = restClient.get().uri("/logs/files").retrieve().body(Map.class);
        if (body == null) return Collections.emptyList();
        Object filesObj = body.get("files");
        if (filesObj instanceof List<?> list) {
            List<Map<String, Object>> result = new java.util.ArrayList<>(list.size());
            for (Object o : list) {
                if (o instanceof Map<?, ?> m) {
                    Map<String, Object> item = new LinkedHashMap<>();
                    for (Map.Entry<?, ?> e : m.entrySet()) {
                        item.put(String.valueOf(e.getKey()), e.getValue());
                    }
                    result.add(item);
                }
            }
            return result;
        }
        return Collections.emptyList();
    }
    
    /**
     * V3-DEFECT-058: 获取记忆完整元数据
     */
    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> getMemoryWithMetadata(String userId, String scopeId, String memId) {
        if (memId == null || memId.isBlank()) {
            return Collections.emptyMap();
        }
        try {
            // 通过内核的 /admin/messages/detail/{msgId} 间接获取
            return restClient.get().uri("/admin/messages/detail/" + memId)
                    .retrieve()
                    .body(Map.class);
        } catch (org.springframework.web.client.ResourceAccessException e) {
            return Collections.emptyMap();
        } catch (Exception e) {
            return Collections.emptyMap();
        }
    }
    
    /**
     * V3-DEFECT-059: 按角色统计用户消息数量
     */
    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> countMessagesByRole(String userId, String scopeId, String sessionId) {
        try {
            // 通过内核的 /admin/messages/stats 获取统计数据
            return restClient.get().uri(uriBuilder -> {
                uriBuilder.path("/admin/messages/stats");
                if (userId != null && !userId.isBlank()) uriBuilder.queryParam("user_id", userId);
                if (scopeId != null && !scopeId.isBlank()) uriBuilder.queryParam("scope_id", scopeId);
                if (sessionId != null && !sessionId.isBlank()) uriBuilder.queryParam("session_id", sessionId);
                return uriBuilder.build();
            }).retrieve().body(Map.class);
        } catch (Exception e) {
            return Collections.emptyMap();
        }
    }
}
