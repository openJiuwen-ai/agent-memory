package com.openjiuwen.memory.opscenter.service.impl;

import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.common.client.MemoryEngineClient;
import com.openjiuwen.memory.common.client.dto.GetUserMemByPageRequest;
import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.spi.PermissionChecker;
import com.openjiuwen.memory.opscenter.domain.MemoryChangeLogSnapshotEntity;
import com.openjiuwen.memory.opscenter.mapper.MemoryChangeLogSnapshotMapper;
import com.openjiuwen.memory.opscenter.service.TraceService;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Service
public class TraceServiceImpl implements TraceService {

    private final MemoryChangeLogSnapshotMapper snapshotMapper;
    private final MemoryEngineClient client;
    private final PermissionChecker permissionChecker;

    public TraceServiceImpl(MemoryChangeLogSnapshotMapper snapshotMapper,
                            MemoryEngineClient client,
                            PermissionChecker permissionChecker) {
        this.snapshotMapper = snapshotMapper;
        this.client = client;
        this.permissionChecker = permissionChecker;
    }

    @Override
    public Map<String, Object> getBundle(String memId, String userId, String scopeId,
                                         String content, String memType, String timestamp, String sourceId) {
        permissionChecker.check("trace:read");
        List<MemoryChangeLogSnapshotEntity> snaps = snapshotsDesc(memId);
        // 前端传入记忆字段，直接构造 MemoryItem，无需翻页查找
        MemoryItem mem = null;
        if (content != null || memType != null || timestamp != null || sourceId != null) {
            mem = new MemoryItem();
            mem.setMemId(memId);
            mem.setContent(content);
            mem.setType(memType);
            mem.setTimestamp(timestamp);
            mem.setSourceId(sourceId);
        }
        String effUser = (userId == null || userId.isBlank()) ? "__default__" : userId;
        String effScope = (scopeId == null || scopeId.isBlank()) ? "__default__" : scopeId;
        Map<String, Object> bundle = new LinkedHashMap<>();
        bundle.put("mem_id", memId);
        bundle.put("current_state", toCurrentState(mem, effUser, effScope));
        // 用记忆的 source_id 经 :8516 get_message_by_id 反查原始对话；无 source_id 或查不到则空
        bundle.put("source_messages", sourceMessages(mem));
        bundle.put("change_history", changeHistory(snaps));
        bundle.put("audit_trail", auditTrail(snaps));
        // 当前 dreaming 不写 mem→mem 血缘
        bundle.put("lineage", Map.of("parents", Collections.emptyList(), "children", Collections.emptyList()));
        return bundle;
    }

    @Override
    public Map<String, Object> getHistory(String memId) {
        permissionChecker.check("trace:read");
        return Map.of("change_history", changeHistory(snapshotsDesc(memId)));
    }

    @Override
    public Map<String, Object> getAudit(String memId) {
        permissionChecker.check("trace:read");
        return Map.of("audit_trail", auditTrail(snapshotsDesc(memId)));
    }

    // —— 内部 ——

    /** 用记忆的 source_id 经 :8516 get_message_by_id 反查原始对话消息。
     *  调用返回 500 → dreaming 产生的记忆（source_id 非真实 message_id），标注来源。 */
    private List<Map<String, Object>> sourceMessages(MemoryItem mem) {
        if (mem == null || mem.getSourceId() == null || mem.getSourceId().isBlank()) {
            return Collections.emptyList();
        }
        try {
            var resp = client.getMessageById(mem.getSourceId());
            if (resp == null || !Boolean.TRUE.equals(resp.getFound())) {
                return Collections.emptyList();
            }
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("role", resp.getRole());
            m.put("content", resp.getContent());
            m.put("time", resp.getTimestamp());
            m.put("message_id", resp.getMessageId());
            return List.of(m);
        } catch (org.springframework.web.client.HttpServerErrorException e) {
            // :8516 返回 500 → source_id 非真实 message_id（dreaming 的 session_id），标注来源
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("role", "dreaming");
            m.put("content", "该记忆由 dreaming 任务自动提炼产生，无原始对话消息");
            return List.of(m);
        } catch (Exception e) {
            // :8516 不可达或路由缺失 → 来源消息留空，不阻断追溯
            return Collections.emptyList();
        }
    }

    private Map<String, Object> toCurrentState(MemoryItem m, String user, String scope) {
        if (m == null) return null;
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("memory_type", m.getType());
        s.put("content", m.getContent());
        s.put("scope_id", scope);
        s.put("user_id", user);
        s.put("created_at", m.getTimestamp());
        s.put("version", null);
        return s;
    }

    private List<MemoryChangeLogSnapshotEntity> snapshotsDesc(String memId) {
        List<MemoryChangeLogSnapshotEntity> asc = snapshotMapper.findByMemIdOrderByCreatedAtAsc(memId);
        if (asc == null || asc.isEmpty()) return List.of();
        List<MemoryChangeLogSnapshotEntity> desc = new ArrayList<>(asc);
        Collections.reverse(desc);
        return desc;
    }

    /** 变更历史：仅 CREATE/UPDATE；version 按时间倒序递减编号（vN=最新）。 */
    private List<Map<String, Object>> changeHistory(List<MemoryChangeLogSnapshotEntity> snaps) {
        long total = snaps.stream()
                .filter(s -> "CREATE".equals(s.getChangeType()) || "UPDATE".equals(s.getChangeType()))
                .count();
        long[] v = { total };
        List<Map<String, Object>> out = new ArrayList<>();
        for (MemoryChangeLogSnapshotEntity s : snaps) {
            if (!("CREATE".equals(s.getChangeType()) || "UPDATE".equals(s.getChangeType()))) continue;
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("time", iso(s.getCreatedAt()));
            e.put("version", "v" + (v[0]--));
            e.put("action", actionText(s.getChangeType()));
            e.put("change_source", s.getChangeType());
            e.put("operator", s.getOperatorId());
            e.put("old_content", s.getOldContent());
            e.put("content", s.getNewContent());
            e.put("reason", s.getReason());
            out.add(e);
        }
        return out;
    }

    /** 操作审计：全部快照（含 DELETE）。 */
    private List<Map<String, Object>> auditTrail(List<MemoryChangeLogSnapshotEntity> snaps) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (MemoryChangeLogSnapshotEntity s : snaps) {
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("time", iso(s.getCreatedAt()));
            e.put("operation", s.getChangeType());
            e.put("operator", s.getOperatorId());
            e.put("operator_type", "system".equalsIgnoreCase(s.getOperatorId()) ? "SYSTEM" : "USER");
            e.put("result", "成功");
            e.put("detail", s.getReason() != null && !s.getReason().isBlank() ? s.getReason() : summarize(s));
            out.add(e);
        }
        return out;
    }

    private String summarize(MemoryChangeLogSnapshotEntity s) {
        if ("DELETE".equals(s.getChangeType())) return "DELETE";
        return s.getOldContent() == null ? "CREATE" : "UPDATE";
    }

    private String actionText(String t) {
        if (t == null) return null;
        return switch (t) {
            case "CREATE" -> "创建";
            case "UPDATE" -> "更新";
            case "DELETE" -> "删除";
            default -> t;
        };
    }

    private String iso(Instant i) {
        return i == null ? null : i.toString();
    }
}
