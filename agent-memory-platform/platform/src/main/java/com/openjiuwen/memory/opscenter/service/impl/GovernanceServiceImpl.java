package com.openjiuwen.memory.opscenter.service.impl;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.dto.DeleteVariablesRequest;
import com.openjiuwen.memory.common.client.dto.GetUserMemByPageRequest;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.spi.AuditRecorder;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.opscenter.domain.GovernancePolicyEntity;
import com.openjiuwen.memory.opscenter.domain.TenantQuotaEntity;
import com.openjiuwen.memory.opscenter.mapper.GovernancePolicyMapper;
import com.openjiuwen.memory.opscenter.mapper.TenantQuotaMapper;
import com.openjiuwen.memory.opscenter.service.GovernanceService;
import com.openjiuwen.memory.opscenter.service.OpsToolService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executor;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.Collectors;

@Service
public class GovernanceServiceImpl implements GovernanceService {

    private static final String DEFAULT_TENANT = "default";
    private static final int SCAN_CAP = 200;          // 翻页上限
    private static final int DUPLICATE_SCAN_CAP = 20; // 相似度扫描条数上限

    private static final Logger log = LoggerFactory.getLogger(GovernanceServiceImpl.class);

    private final GovernancePolicyMapper policyMapper;
    private final TenantQuotaMapper quotaMapper;
    private final MemoryEngineClient client;
    private final OpsToolService opsTool;
    private final PermissionChecker permissionChecker;
    private final AuditRecorder auditRecorder;
    private final ObjectMapper objectMapper;
    private final Executor scanExecutor;

    @Value("${platform.governance.forbidden-variables:phone,email,id_card,mobile,idcard}")
    private String forbiddenVariablesCsv;

    @Value("${platform.governance.stale-days:90}")
    private long staleDays;

    public GovernanceServiceImpl(GovernancePolicyMapper policyMapper,
                                 TenantQuotaMapper quotaMapper,
                                 MemoryEngineClient client,
                                 OpsToolService opsTool,
                                 PermissionChecker permissionChecker,
                                 AuditRecorder auditRecorder,
                                 ObjectMapper objectMapper,
                                 @Qualifier("auditLogExecutor") Executor scanExecutor) {
        this.policyMapper = policyMapper;
        this.quotaMapper = quotaMapper;
        this.client = client;
        this.opsTool = opsTool;
        this.permissionChecker = permissionChecker;
        this.auditRecorder = auditRecorder;
        this.objectMapper = objectMapper;
        this.scanExecutor = scanExecutor;
    }

    // —— 策略 ——

    @Override
    public Map<String, Object> getStrategy(String adminUserId) {
        permissionChecker.check("governance:read");
        String tenant = adminUserId == null || adminUserId.isBlank() ? DEFAULT_TENANT : adminUserId;
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("lifecycle", loadSection(tenant, "LIFECYCLE", defaultLifecycle()));
        out.put("quality", loadSection(tenant, "QUALITY", defaultQuality()));
        out.put("compliance", loadSection(tenant, "COMPLIANCE", defaultCompliance()));
        // quota：策略 + 配额上限 + 当前用量
        out.put("quota", loadQuotaSection(tenant));
        return out;
    }

    @Override
    public void saveStrategy(String adminUserId, Map<String, Object> strategy, String operator) {
        permissionChecker.check("governance:write");
        String tenant = adminUserId == null || adminUserId.isBlank() ? DEFAULT_TENANT : adminUserId;
        upsertPolicy(tenant, "LIFECYCLE", "生命周期策略", section(strategy, "lifecycle", defaultLifecycle()), true, operator);
        upsertPolicy(tenant, "QUALITY", "质量评分策略", section(strategy, "quality", defaultQuality()), true, operator);
        upsertPolicy(tenant, "COMPLIANCE", "合规检查策略", section(strategy, "compliance", defaultCompliance()), true, operator);
        Map<String, Object> quotaCfg = section(strategy, "quota", defaultQuota());
        upsertPolicy(tenant, "QUOTA", "配额策略", quotaCfg, true, operator);
        upsertQuota(tenant, quotaCfg);
        auditRecorder.record(new AuditRecorder.AuditEvent(operator, "PUT", "/ops/governance/strategy", "success", null, "保存治理策略"));
    }

    // —— 扫描 ——

    @Override
    public Map<String, Object> scan(String scanType, String userId, String scopeId, Double threshold) {
        permissionChecker.check("governance:read");
        List<MemoryItem> items = listAllMemories(userId, scopeId, SCAN_CAP);
        AtomicLong duplicate = new AtomicLong(0);
        long stale = 0;
        long empty = 0;
        List<Map<String, Object>> dupItems = new CopyOnWriteArrayList<>();
        if ("duplicate".equalsIgnoreCase(scanType) || scanType == null || scanType.isBlank()) {
            double th = threshold == null ? 0.85 : threshold;
            List<MemoryItem> pool = items.size() > DUPLICATE_SCAN_CAP ? items.subList(0, DUPLICATE_SCAN_CAP) : items;
            // 并行扫描：每条记忆的相似度搜索独立提交到专用线程池
            List<CompletableFuture<Void>> futures = new ArrayList<>();
            for (MemoryItem m : pool) {
                if (m.getContent() == null || m.getContent().isBlank()) continue;
                futures.add(CompletableFuture.runAsync(() -> {
                    var req = new com.openjiuwen.memory.common.client.dto.SearchMemoryRequest();
                    req.setQuery(m.getContent());
                    req.setNum(5);
                    req.setUserId(userId);
                    req.setScopeId(scopeId);
                    req.setThreshold(th);
                    List<MemoryItem> hits = client.searchMemory(req);
                    if (hits != null) {
                        for (MemoryItem h : hits) {
                            if (h.getMemId() != null && !h.getMemId().equals(m.getMemId())
                                    && h.getScore() != null && h.getScore() >= th) {
                                duplicate.incrementAndGet();
                                Map<String, Object> pair = new LinkedHashMap<>();
                                pair.put("a", m.getMemId());
                                pair.put("b", h.getMemId());
                                pair.put("score", h.getScore());
                                dupItems.add(pair);
                            }
                        }
                    }
                }, scanExecutor));
            }
            CompletableFuture.allOf(futures.toArray(new CompletableFuture[0])).join();
        }
        Instant staleBefore = Instant.now().minus(staleDays, ChronoUnit.DAYS);
        for (MemoryItem m : items) {
            if (m.getContent() == null || m.getContent().isBlank()) { empty++; continue; }
            if ("stale".equalsIgnoreCase(scanType) || scanType == null || scanType.isBlank()) {
                if (m.getTimestamp() != null) {
                    try {
                        if (Instant.parse(m.getTimestamp()).isBefore(staleBefore)) stale++;
                    } catch (Exception e) { log.warn("解析时间戳失败 timestamp={}", m.getTimestamp(), e); }
                }
            }
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("scan_type", scanType == null ? "all" : scanType);
        out.put("duplicate_count", duplicate.get());
        out.put("stale_count", stale);
        out.put("empty_count", empty);
        out.put("items", dupItems);
        return out;
    }

    // —— 合规 ——

    @Override
    public Map<String, Object> compliance(String userId, String scopeId, boolean autoFix) {
        permissionChecker.check("governance:read");
        var greq = new com.openjiuwen.memory.common.client.dto.GetVariablesRequest();
        greq.setUserId(userId);
        greq.setScopeId(scopeId);
        Map<String, String> vars = client.getVariables(greq);
        if (vars == null) vars = Collections.emptyMap();
        List<String> forbidden = forbiddenList();
        List<Map<String, Object>> violations = new ArrayList<>();
        for (Map.Entry<String, String> e : vars.entrySet()) {
            if (matchesForbidden(e.getKey(), forbidden)) {
                Map<String, Object> v = new LinkedHashMap<>();
                v.put("name", e.getKey());
                v.put("value", e.getValue());
                v.put("reason", "命中 forbidden_variables");
                violations.add(v);
            }
        }
        int deleted = 0;
        if (autoFix && !violations.isEmpty()) {
            List<String> names = violations.stream().map(v -> (String) v.get("name")).collect(Collectors.toList());
            var dreq = new DeleteVariablesRequest();
            dreq.setUserId(userId);
            dreq.setScopeId(scopeId);
            dreq.setNames(names);
            client.deleteVariables(dreq);
            deleted = names.size();
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("forbidden_variables", forbidden);
        out.put("violation_count", violations.size());
        out.put("violations", violations);
        out.put("auto_fixed", deleted);
        return out;
    }

    // —— 治理页聚合 ——

    @Override
    public Map<String, Object> getGovernancePage(String adminUserId, String userId, String scopeId) {
        permissionChecker.check("governance:read");
        Map<String, Object> scanResults = scan(null, userId, scopeId, null);
        Map<String, Object> complianceStatus = compliance(userId, scopeId, false);
        long currentMemories = currentMemoryCount(userId, scopeId);
        TenantQuotaEntity q = ensureQuota(adminUserId == null || adminUserId.isBlank() ? DEFAULT_TENANT : adminUserId);
        long limit = q.getMaxMemoriesPerUser() == null ? 100000 : q.getMaxMemoriesPerUser();
        double usagePct = limit > 0 ? Math.min(100.0, currentMemories * 100.0 / limit) : 0;

        long lastScanIssues = toLong(scanResults.get("duplicate_count")) + toLong(scanResults.get("stale_count")) + toLong(scanResults.get("empty_count"));
        long violations = toLong(complianceStatus.get("violation_count"));

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("governance_summary", Map.of(
                "active_cleanup_tasks", 0,
                "last_scan_issues", lastScanIssues,
                "compliance_violations", violations,
                "quota_usage_percent", Math.round(usagePct * 100) / 100.0));
        out.put("scan_results", Map.of(
                "duplicate_count", toLong(scanResults.get("duplicate_count")),
                "stale_count", toLong(scanResults.get("stale_count")),
                "empty_count", toLong(scanResults.get("empty_count"))));
        out.put("compliance_status", Map.of(
                "forbidden_variables", complianceStatus.get("forbidden_variables"),
                "violation_count", violations));
        out.put("quota_status", Map.of(
                "limit", limit,
                "usage", currentMemories,
                "usage_percent", Math.round(usagePct * 100) / 100.0));
        return out;
    }

    // —— 内部 ——

    private Map<String, Object> loadSection(String tenant, String type, Map<String, Object> fallback) {
        List<GovernancePolicyEntity> ps = policyMapper.findByType(type, tenant);
        if (ps == null || ps.isEmpty()) return fallback;
        GovernancePolicyEntity p = ps.get(0);
        Map<String, Object> cfg = parseConfig(p.getPolicyConfig());
        cfg.put("enabled", p.getIsEnabled() == null ? true : p.getIsEnabled());
        return cfg;
    }

    private Map<String, Object> loadQuotaSection(String tenant) {
        TenantQuotaEntity q = ensureQuota(tenant);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("enabled", true);
        m.put("maxScopes", q.getMaxScopes());
        m.put("maxMemoriesPerUser", q.getMaxMemoriesPerUser());
        m.put("maxDailyMessages", q.getMaxMessagesPerDay());
        m.put("currentMemories", 0); // 由调用方按 user/scope 填（见 getGovernancePage）
        return m;
    }

    private void upsertPolicy(String tenant, String type, String name, Map<String, Object> cfg, boolean enabled, String operator) {
        List<GovernancePolicyEntity> existing = policyMapper.findByType(type, tenant);
        GovernancePolicyEntity e = (existing != null && !existing.isEmpty()) ? existing.get(0) : new GovernancePolicyEntity();
        boolean isNew = e.getId() == null;
        e.setAdminUserId(tenant);
        e.setPolicyType(type);
        e.setPolicyName(name);
        e.setPolicyConfig(toJson(cfg));
        e.setIsEnabled(enabled);
        if (isNew) {
            e.setCreatedBy(operator == null ? "system" : operator);
            e.setCreatedAt(Instant.now());
            policyMapper.insert(e);
        } else {
            e.setUpdatedAt(Instant.now());
            policyMapper.updateById(e);
        }
    }

    private void upsertQuota(String tenant, Map<String, Object> cfg) {
        TenantQuotaEntity q = ensureQuota(tenant);
        if (cfg != null) {
            q.setMaxScopes(intOr(cfg.get("maxScopes"), 100));
            q.setMaxMemoriesPerUser(intOr(cfg.get("maxMemoriesPerUser"), 100000));
            q.setMaxMessagesPerDay(intOr(cfg.get("maxDailyMessages"), 1000000));
        }
        q.setUpdatedAt(Instant.now());
        if (q.getId() == null) {
            quotaMapper.insert(q);
        } else {
            quotaMapper.updateById(q);
        }
    }

    private TenantQuotaEntity ensureQuota(String tenant) {
        TenantQuotaEntity q = quotaMapper.findByAdminUserId(tenant);
        if (q == null) {
            q = new TenantQuotaEntity();
            q.setAdminUserId(tenant);
        }
        return q;
    }

    private long currentMemoryCount(String userId, String scopeId) {
        Map<String, Object> c = opsTool.memoryCount(userId, scopeId, null);
        Object cnt = c == null ? null : c.get("count");
        if (cnt instanceof Number n) return n.longValue();
        return 0;
    }

    private List<MemoryItem> listAllMemories(String userId, String scopeId, int cap) {
        List<MemoryItem> all = new ArrayList<>();
        int pageSize = 100;
        int pageIdx = 1;
        while (pageIdx <= 1000 && all.size() < cap) {
            var req = new GetUserMemByPageRequest();
            req.setUserId(userId);
            req.setScopeId(scopeId);
            req.setPageSize(pageSize);
            req.setPageIdx(pageIdx);
            PageResult<MemoryItem> page = client.getUserMemByPage(req);
            if (page == null || page.items() == null || page.items().isEmpty()) break;
            all.addAll(page.items());
            if (page.items().size() < pageSize) break;
            pageIdx++;
        }
        return all.size() > cap ? all.subList(0, cap) : all;
    }

    private List<String> forbiddenList() {
        if (forbiddenVariablesCsv == null || forbiddenVariablesCsv.isBlank()) return List.of();
        return Arrays.stream(forbiddenVariablesCsv.split(",")).map(String::trim)
                .filter(s -> !s.isEmpty()).map(String::toLowerCase).collect(Collectors.toList());
    }

    private boolean matchesForbidden(String name, List<String> forbidden) {
        if (name == null) return false;
        String n = name.toLowerCase();
        return forbidden.stream().anyMatch(n::contains);
    }

    private Map<String, Object> parseConfig(String json) {
        if (json == null || json.isBlank()) return new LinkedHashMap<>();
        try {
            return objectMapper.readValue(json, new TypeReference<Map<String, Object>>() {});
        } catch (Exception e) {
            log.warn("parseConfig: failed to parse JSON config, returning empty map", e);
            return new LinkedHashMap<>();
        }
    }

    private String toJson(Map<String, Object> m) {
        if (m == null) return "{}";
        try {
            return objectMapper.writeValueAsString(m);
        } catch (Exception e) {
            return "{}";
        }
    }

    private Map<String, Object> section(Map<String, Object> strategy, String key, Map<String, Object> fallback) {
        if (strategy == null) return fallback;
        Object v = strategy.get(key);
        if (v instanceof Map<?, ?> raw) {
            // 类型安全转换：仅当 key 为 String 时才接受
            Map<String, Object> result = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : raw.entrySet()) {
                if (entry.getKey() instanceof String k) {
                    result.put(k, entry.getValue());
                } else {
                    log.warn("section: non-String key '{}' in config section '{}', skipping", entry.getKey(), key);
                }
            }
            return result;
        }
        return fallback;
    }

    private static long toLong(Object o) {
        return o instanceof Number n ? n.longValue() : 0;
    }

    private static int intOr(Object o, int def) {
        return o instanceof Number n ? n.intValue() : def;
    }

    private Map<String, Object> defaultLifecycle() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("enabled", true);
        m.put("ttlDays", 90);
        return m;
    }

    private Map<String, Object> defaultQuality() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("enabled", true);
        m.put("threshold", 60);
        return m;
    }

    private Map<String, Object> defaultCompliance() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("enabled", true);
        return m;
    }

    private Map<String, Object> defaultQuota() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("enabled", true);
        m.put("maxScopes", 100);
        m.put("maxMemoriesPerUser", 100000);
        m.put("maxDailyMessages", 1000000);
        return m;
    }
}
