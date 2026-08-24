package com.openjiuwen.memory.webui.service.impl;

import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.client.dto.MemoryType;
import com.openjiuwen.memory.configcenter.domain.InstanceConfigEntity;
import com.openjiuwen.memory.configcenter.domain.TenantScopeConfigEntity;
import com.openjiuwen.memory.configcenter.service.ConfigTemplateService;
import com.openjiuwen.memory.configcenter.service.InstanceConfigService;
import com.openjiuwen.memory.configcenter.service.KernelConfigService;
import com.openjiuwen.memory.configcenter.service.TenantScopeConfigService;
import com.openjiuwen.memory.logcenter.service.MessageLogService;
import com.openjiuwen.memory.logcenter.service.OperationLogService;
import com.openjiuwen.memory.opscenter.service.GovernanceService;
import com.openjiuwen.memory.opscenter.service.MemoryManageService;
import com.openjiuwen.memory.opscenter.service.OpsToolService;
import com.openjiuwen.memory.opscenter.service.TaskService;
import com.openjiuwen.memory.opscenter.service.TraceService;
import com.openjiuwen.memory.webui.service.UiAggregatorService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;
import java.util.*;

/**
 * Web UI 聚合服务实现（§8.2）— 2026-07-17 P0-3 v2 重构
 * <p>
 * 从原 UiController 抽取的聚合逻辑，每个页面一次请求获取所有需要的数据。
 * 所有 catch 块均记录 WARN 日志，避免静默吞异常。
 * <p>
 * 配置中心部分：删除 ScopeConfigService，改为 ConfigTemplateService（系统/自定义模板）+
 * InstanceConfigService（实例级单例）+ TenantScopeConfigService（租户快照）。
 */
@Service
public class UiAggregatorServiceImpl implements UiAggregatorService {

    private static final Logger log = LoggerFactory.getLogger(UiAggregatorServiceImpl.class);

    // —— 常量提取（消除魔法值）——

    private static final String DEFAULT_SCOPE_ID = "__default__";
    private static final String DEFAULT_SCOPE_NAME = "默认";
    private static final String DEFAULT_KV_STORE = "db";
    private static final String DEFAULT_DB_STORE = "default";
    private static final String DEFAULT_VECTOR_STORE = "chroma";
    private static final String HEALTH_STATUS_HEALTHY = "healthy";
    private static final String HEALTH_STATUS_UNAVAILABLE = "unavailable";
    private static final String KERNEL_CONFIG_SOURCE = "kernel .env";
    private static final String LOG_TAB_OPERATIONS = "operations";
    private static final String LOG_TAB_RUNTIME = "runtime";
    private static final String LOG_TAB_MESSAGES = "messages";
    private static final List<String> LOG_TABS = List.of(LOG_TAB_OPERATIONS, LOG_TAB_RUNTIME, LOG_TAB_MESSAGES);
    private static final List<String> MEMORY_TYPE_LIST = List.of(
            MemoryType.USER_PROFILE,
            MemoryType.SEMANTIC_MEMORY,
            MemoryType.EPISODIC_MEMORY,
            MemoryType.SUMMARY,
            MemoryType.VARIABLE,
            MemoryType.MIDDLE_TERM_MEMORY
    );

    private final MemoryManageService memoryManageService;
    private final OpsToolService opsToolService;
    private final TaskService taskService;
    private final TraceService traceService;
    private final GovernanceService governanceService;
    private final KernelConfigService kernelConfigService;
    private final ConfigTemplateService configTemplateService;
    private final InstanceConfigService instanceConfigService;
    private final TenantScopeConfigService tenantScopeConfigService;
    private final OperationLogService operationLogService;
    private final MessageLogService messageLogService;

    public UiAggregatorServiceImpl(MemoryManageService memoryManageService,
                                   OpsToolService opsToolService,
                                   TaskService taskService,
                                   TraceService traceService,
                                   GovernanceService governanceService,
                                   KernelConfigService kernelConfigService,
                                   ConfigTemplateService configTemplateService,
                                   InstanceConfigService instanceConfigService,
                                   TenantScopeConfigService tenantScopeConfigService,
                                   OperationLogService operationLogService,
                                   MessageLogService messageLogService) {
        this.memoryManageService = memoryManageService;
        this.opsToolService = opsToolService;
        this.taskService = taskService;
        this.traceService = traceService;
        this.governanceService = governanceService;
        this.kernelConfigService = kernelConfigService;
        this.configTemplateService = configTemplateService;
        this.instanceConfigService = instanceConfigService;
        this.tenantScopeConfigService = tenantScopeConfigService;
        this.operationLogService = operationLogService;
        this.messageLogService = messageLogService;
    }

    // —— §8.2.2 记忆浏览页 ——

    @Override
    public Map<String, Object> buildMemoryBrowse(String scopeId, String userId, String memoryType, int pageSize, int pageIdx) {
        Map<String, Object> data = new LinkedHashMap<>();

        // 记忆列表
        try {
            PageResult<MemoryItem> page = memoryManageService.list(userId, scopeId, memoryType, pageIdx, pageSize);
            data.put("memories", page.items() != null ? page.items() : Collections.emptyList());
            data.put("total", page.total());
        } catch (Exception e) {
            log.warn("MemoryBrowse: list failed, userId={}, scopeId={}", userId, scopeId, e);
            data.put("memories", Collections.emptyList());
            data.put("total", 0);
        }

        // 记忆类型列表
        data.put("memory_types", MEMORY_TYPE_LIST);

        // Scope 信息
        Map<String, Object> scopeInfo = new LinkedHashMap<>();
        scopeInfo.put("scope_id", scopeId == null ? DEFAULT_SCOPE_ID : scopeId);
        scopeInfo.put("scope_name", scopeId == null ? DEFAULT_SCOPE_NAME : scopeId);
        scopeInfo.put("has_config", false);
        data.put("scope_info", scopeInfo);

        // 变量
        try {
            Map<String, String> variables = memoryManageService.getVariables(userId, scopeId, null);
            data.put("variables", variables != null ? variables : Collections.emptyMap());
        } catch (Exception e) {
            log.warn("MemoryBrowse: getVariables failed, userId={}, scopeId={}", userId, scopeId, e);
            data.put("variables", Collections.emptyMap());
        }

        return data;
    }

    // —— §8.2.3 配置管理页（P0-3 v2：模板 + 实例 + 租户快照）——

    @Override
    public Map<String, Object> buildConfigPage(String adminUserId, String scopeId) {
        Map<String, Object> data = new LinkedHashMap<>();

        // 1) 内核配置（.env，Push 模型）
        try {
            Map<String, Object> kernelConfig = kernelConfigService.getKernelConfig();
            if (kernelConfig != null) {
                kernelConfig.put("editable", true);
                kernelConfig.put("source", KERNEL_CONFIG_SOURCE);
            }
            data.put("kernel_config", kernelConfig != null ? kernelConfig : Collections.emptyMap());
        } catch (Exception e) {
            log.warn("ConfigPage: getKernelConfig failed", e);
            data.put("kernel_config", Collections.emptyMap());
        }

        // 2) 配置模板：系统默认（is_builtin=true）+ 自定义（is_builtin=false）
        try {
            data.put("config_templates", configTemplateService.list(null, null));
        } catch (Exception e) {
            log.warn("ConfigPage: configTemplateService.list failed", e);
            data.put("config_templates", Collections.emptyList());
        }

        // 3) 实例级配置（INSTANCE 模板应用结果，单例）
        try {
            InstanceConfigEntity instance = instanceConfigService.get();
            data.put("instance_config", instance);
        } catch (Exception e) {
            log.warn("ConfigPage: instanceConfigService.get failed", e);
            data.put("instance_config", null);
        }

        // 4) 租户级 Scope 配置快照列表（所有租户）
        try {
            data.put("tenant_scope_configs",
                    tenantScopeConfigService.listAll() != null ? tenantScopeConfigService.listAll() : Collections.emptyList());
        } catch (Exception e) {
            log.warn("ConfigPage: tenantScopeConfigService.listAll failed", e);
            data.put("tenant_scope_configs", Collections.emptyList());
        }

        // 5) 内核状态
        Map<String, Object> kernelStatus = new LinkedHashMap<>();
        try {
            Map<String, Object> health = opsToolService.healthProbe();
            kernelStatus.put("running", HEALTH_STATUS_HEALTHY.equals(health == null ? null : health.get("status")));
        } catch (Exception e) {
            log.warn("ConfigPage: healthProbe failed", e);
            kernelStatus.put("running", false);
        }
        kernelStatus.put("last_restart_at", null);
        data.put("kernel_status", kernelStatus);

        return data;
    }

    // —— §8.2.4 日志页 ——

    @Override
    public Map<String, Object> buildLogsPage(String adminUserId, String tab, int page, int size) {
        Map<String, Object> data = new LinkedHashMap<>();

        data.put("tabs", LOG_TABS);

        Instant weekAgo = Instant.now().minus(Duration.ofDays(7));

        try {
            switch (tab) {
                case LOG_TAB_RUNTIME -> {
                    // 运行日志不入库（§6.3.2），服务层无 DB 分页数据；
                    // 前端通过 RuntimeLogController /tail 瞬时查询内核日志。
                    data.put("logs", Collections.emptyList());
                    data.put("total", 0);
                    data.put("hint", "runtime logs are not stored in DB, use /api/v1/logs/runtime/tail instead");
                }
                case LOG_TAB_MESSAGES -> {
                    var logPage = messageLogService.queryLogs(adminUserId, null, null, null, weekAgo, null, page, size);
                    data.put("logs", logPage.getRecords());
                    data.put("total", logPage.getTotal());
                }
                default -> {
                    var logPage = operationLogService.queryLogs(adminUserId, null, null, null, weekAgo, null, page, size);
                    data.put("logs", logPage.getRecords());
                    data.put("total", logPage.getTotal());
                }
            }
        } catch (Exception e) {
            log.warn("LogsPage: queryLogs failed, adminUserId={}, tab={}", adminUserId, tab, e);
            data.put("logs", Collections.emptyList());
            data.put("total", 0);
        }

        // 统计（运行日志不入库，by_level 统计已移除；保留操作日志 by_type 统计）
        Map<String, Object> statistics = new LinkedHashMap<>();
        try {
            List<Map<String, Object>> byType = operationLogService.statsByType(adminUserId, weekAgo, null);
            Map<String, Long> typeMap = new LinkedHashMap<>();
            if (byType != null) {
                for (Map<String, Object> row : byType) {
                    String type = row.get("itemType") == null ? "UNKNOWN" : String.valueOf(row.get("itemType"));
                    long count = 0;
                    Object cnt = row.get("count");
                    if (cnt instanceof Number n) count = n.longValue();
                    typeMap.put(type, count);
                }
            }
            statistics.put("by_type", typeMap);
        } catch (Exception e) {
            log.warn("LogsPage: statsByType failed, adminUserId={}", adminUserId, e);
            statistics.put("by_type", Collections.emptyMap());
        }
        data.put("statistics", statistics);

        return data;
    }

    // —— §8.2.5 运维页 ——

    @Override
    public Map<String, Object> buildOpsPage(String adminUserId, String scopeId, String userId) {
        Map<String, Object> data = new LinkedHashMap<>();

        // 健康状态
        try {
            data.put("health", opsToolService.healthProbe());
        } catch (Exception e) {
            log.warn("OpsPage: healthProbe failed", e);
            data.put("health", Map.of("status", HEALTH_STATUS_UNAVAILABLE));
        }

        // 任务列表
        try {
            data.put("tasks", taskService.listTasks(adminUserId));
        } catch (Exception e) {
            log.warn("OpsPage: listTasks failed, adminUserId={}", adminUserId, e);
            data.put("tasks", Collections.emptyList());
        }

        // Dreaming 状态
        try {
            data.put("dreaming_status", taskService.dreamingStatus(adminUserId, scopeId, userId));
        } catch (Exception e) {
            log.warn("OpsPage: dreamingStatus failed, adminUserId={}, scopeId={}", adminUserId, scopeId, e);
            data.put("dreaming_status", Collections.emptyMap());
        }

        // 治理摘要 — 委托 GovernanceService.getGovernancePage() 获取真实数据
        try {
            Map<String, Object> govPage = governanceService.getGovernancePage(adminUserId, userId, scopeId);
            data.put("governance_summary", govPage != null ? govPage : Collections.emptyMap());
        } catch (Exception e) {
            log.warn("OpsPage: getGovernancePage failed, adminUserId={}, scopeId={}", adminUserId, scopeId, e);
            Map<String, Object> fallback = new LinkedHashMap<>();
            fallback.put("quota_usage_percent", 0.0);
            fallback.put("active_cleanup_tasks", 0);
            fallback.put("last_scan_issues", 0);
            fallback.put("compliance_violations", 0);
            data.put("governance_summary", fallback);
        }

        // 追溯摘要 — TraceService 当前无聚合统计接口，暂用占位值
        Map<String, Object> traceSummary = new LinkedHashMap<>();
        traceSummary.put("total_traced_memories", 0);
        traceSummary.put("recent_corrections", 0);
        data.put("trace_summary", traceSummary);

        return data;
    }

    // —— §8.2.7 记忆追溯页 ——

    @Override
    public Map<String, Object> buildTracePage(String memId, String userId, String scopeId) {
        Map<String, Object> data = new LinkedHashMap<>();

        if (memId == null || memId.isBlank()) {
            // 不传 mem_id 则显示追溯入口
            data.put("mem_id", null);
            data.put("message", "请提供 mem_id 参数查询追溯信息");
            return data;
        }

        // 聚合调用 TraceService 的全链路追溯接口
        try {
            Map<String, Object> bundle = traceService.getBundle(memId, userId, scopeId, null, null, null, null);
            if (bundle != null) {
                data.putAll(bundle);
            }
            data.putIfAbsent("mem_id", memId);
        } catch (Exception e) {
            log.warn("TracePage: getBundle failed, memId={}, userId={}", memId, userId, e);
            data.put("mem_id", memId);
            data.put("error", "追溯查询失败: " + e.getMessage());
            data.put("current_state", Collections.emptyMap());
            data.put("source_messages", Collections.emptyList());
            data.put("change_history", Collections.emptyList());
            data.put("audit_trail", Collections.emptyList());
            data.put("lineage", Map.of("parents", Collections.emptyList(), "children", Collections.emptyList()));
        }

        return data;
    }

    // —— 内部工具 ——

    private Map<String, String> defaultStoreTypes() {
        return Map.of("kv", DEFAULT_KV_STORE, "db", DEFAULT_DB_STORE, "vector", DEFAULT_VECTOR_STORE);
    }
}
