package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.common.client.dto.MemoryItem;
import com.openjiuwen.memory.common.PageResult;

import java.util.List;
import java.util.Map;

/**
 * 记忆服务列表管理（功能5）。按 user/scope/type 过滤查询，支持新增/修改/删除；变量亦属记忆内容。
 */
public interface MemoryManageService {

    PageResult<MemoryItem> list(String userId, String scopeId, String memoryType, int pageIdx, int pageSize);

    /** 单条详情：:8516 无 get_mem_by_id，按 mem_id 在所属 user/scope 分页内定位（降级） */
    MemoryItem detail(String memId, String userId, String scopeId);

    /** 新增记忆（从消息抽取）；enable_* 由 FeatureFlagService.resolve 注入 */
    Object create(String userId, String scopeId, List<Map<String, String>> messages,
                  List<com.openjiuwen.memory.common.client.dto.MemVariable> memVariables, String operator, String reason);

    /** 修改记忆内容（含变更留痕）。oldContent 由前端传入，避免后端翻页查找。 */
    Object update(String memId, String memory, String oldContent, String userId, String scopeId, String operator, String reason);

    /** 单条删除：:8516 未暴露 delete_mem_by_id → 抛 GapException。oldContent 由前端传入用于快照。 */
    Object deleteOne(String memId, String userId, String scopeId, String oldContent, String operator, String reason);

    /** 按 scope 删除（二次确认 confirmToken） */
    Object deleteByScope(String scopeId, String confirmToken, String operator, String reason);

    /** 批量删除：真实版 :8516 未暴露 batch_delete_mem → 抛 GapException；mock 可用 */
    Object batchDelete(List<String> memIds, String userId, String scopeId, String operator, String reason);

    // —— 变量（记忆内容） ——
    Map<String, String> getVariables(String userId, String scopeId, List<String> names);

    Object updateVariables(String userId, String scopeId, Map<String, String> variables, String operator);

    Object deleteVariables(String userId, String scopeId, List<String> names, String operator);
}
