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
import com.openjiuwen.memory.common.exception.GapException;

import java.util.List;
import java.util.Map;

/**
 * 记忆服务 :8516 调用收敛点。所有对 :8516 的 HTTP 调用经此接口。
 * <p>
 * 现可实现的端点全部对应线上 :8516 的 10 个接口；缺口方法以 default 方法抛 {@link GapException}，
 * 待记忆服务补端点后在实现类中覆写即可激活，无需改 Service/Controller。
 */
public interface MemoryEngineClient {

    // —— 记忆列表 / 检索 ——
    PageResult<MemoryItem> getUserMemByPage(GetUserMemByPageRequest req);

    List<MemoryItem> searchMemory(SearchMemoryRequest req);

    List<MemoryItem> searchHistorySummary(SearchHistorySummaryRequest req);

    // —— 记忆写入 / 修改 ——
    RawResponses.StatusMessage addMessages(AddMessagesRequest req);

    RawResponses.StatusMessage updateMemById(UpdateMemoryRequest req);

    // —— 记忆删除 ——
    RawResponses.DeleteResult deleteMemByScope(DeleteByScopeRequest req);

    /** ⚠️ 缺口：:8516 未暴露 delete_mem_by_id，待记忆服务补端点 */
    default RawResponses.DeleteResult deleteMemById(String memId, String userId, String scopeId) {
        throw new GapException("delete_mem_by_id 未由 :8516 暴露，待记忆服务补 /delete_mem_by_id/");
    }

    /** ⚠️ 缺口：按 user_id 删除未暴露 */
    default RawResponses.DeleteResult deleteMemByUserId(String userId, String scopeId) {
        throw new GapException("delete_mem_by_user_id 未由 :8516 暴露，待记忆服务补 /delete_mem_by_user_id/");
    }

    /** ⚠️ 缺口：批量删除未暴露 */
    default RawResponses.DeleteResult batchDeleteMem(List<String> memIds, String userId, String scopeId) {
        throw new GapException("batch_delete_mem 未由 :8516 暴露，待记忆服务补 /batch_delete_mem/");
    }

    // —— 变量（记忆内容） ——
    Map<String, String> getVariables(GetVariablesRequest req);

    RawResponses.StatusMessage updateVariables(UpdateVariablesRequest req);

    RawResponses.DeleteResult deleteVariables(DeleteVariablesRequest req);

    // —— 健康 ——
    RawResponses.Health health();

    /**
     * 按 message_id 反查原始对话消息（供 F7 source_messages 用）。
     * default 返回 null：MockMemoryEngineClient 继承占位；DefaultMemoryEngineClient 覆写为真实 POST。
     */
    default RawResponses.GetMessageResponse getMessageById(String messageId) {
        return null;
    }

    // —— 缺口占位：Admin / Config / Dreaming / 迁移 ——
    default Object restartKernel() {
        throw new GapException("内核重启 API 未由 :8516 暴露，待记忆服务补 /admin/restart");
    }

    default Object reloadConfig() {
        throw new GapException("配置热加载 API 未由 :8516 暴露，待记忆服务补 /admin/reload-config");
    }

    default Object clearCache() {
        throw new GapException("清缓存 API 未由 :8516 暴露，待记忆服务补 /admin/clear-cache");
    }

    default Object rebuildIndex() {
        throw new GapException("重建索引 API 未由 :8516 暴露，待记忆服务补 /admin/rebuild-index");
    }

    // —— Scope 发现 ——

    /**
     * 列出指定租户下所有已注册的 Scope 名称。
     * ⚠️ 缺口：:8516 未暴露 list_scopes，待记忆服务补端点。
     */
    default List<String> listScopes(String adminUserId) {
        throw new GapException("list_scopes 未由 :8516 暴露，待记忆服务补 /admin/list_scopes");
    }

    // —— Scope 级配置管理（热加载，通过内核 API）——

    /**
     * 获取 Scope 级配置（内核 POST /get_scope_config）。
     * 返回 Map（对应内核 MemoryScopeConfig），null 表示 Scope 级配置不存在（需回退继承链）。
     */
    default Map<String, Object> getScopeConfig(String scopeId) {
        throw new GapException("get_scope_config 未由 :8516 暴露，待记忆服务补 /get_scope_config/");
    }

    /**
     * 写入/更新 Scope 级配置（内核 POST /set_scope_config），热加载即时生效。
     */
    default Map<String, Object> setScopeConfig(String scopeId, Map<String, Object> config) {
        throw new GapException("set_scope_config 未由 :8516 暴露，待记忆服务补 /set_scope_config/");
    }

    /**
     * 删除 Scope 级配置（内核 POST /delete_scope_config），回退到继承链。
     */
    default boolean deleteScopeConfig(String scopeId) {
        throw new GapException("delete_scope_config 未由 :8516 暴露，待记忆服务补 /delete_scope_config/");
    }

    // —— 内核配置管理（Push 模型，写 .env + 重启）——

    /**
     * 获取内核当前配置（内核 GET /admin/config），敏感字段脱敏。
     */
    default Map<String, Object> getKernelConfig() {
        throw new GapException("内核配置查询 API 未由 :8516 暴露，待记忆服务补 GET /admin/config");
    }

    /**
     * Push 配置到内核 .env（内核 PUT /admin/config），拒绝架构参数。
     */
    default Map<String, Object> pushKernelConfig(Map<String, String> updates) {
        throw new GapException("内核配置 Push API 未由 :8516 暴露，待记忆服务补 PUT /admin/config");
    }

    // —— 运行日志管理（内核 HTTP API）——

    /**
     * 获取内核运行日志（内核 GET /admin/logs）。
     */
    default List<Map<String, Object>> getKernelLogs(String level, String eventType, int limit) {
        throw new GapException("内核日志查询 API 未由 :8516 暴露，待记忆服务补 GET /admin/logs");
    }

    /**
     * 设置内核日志级别（内核 POST /admin/set_log_level）。
     */
    default boolean setKernelLogLevel(String level, String module) {
        throw new GapException("内核日志级别设置 API 未由 :8516 暴露，待记忆服务补 POST /admin/set_log_level");
    }

    /**
     * 瞬时查询内核运行日志（内核 GET /logs/tail）。
     * 不入库，服务层直接转发内核返回的最近 N 行日志。
     *
     * @param lines     读取行数（默认500，最大2000）
     * @param level     日志级别过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL，可空）
     * @param eventType 事件类型过滤（LogEventType 枚举值，可空）
     * @return 日志行列表（每行一条原始日志文本或结构化字段）
     */
    default List<String> tailKernelLogs(int lines, String level, String eventType) {
        throw new GapException("内核日志 tail API 未暴露，待记忆服务补 GET /logs/tail");
    }

    /**
     * 按文件名下载内核运行日志（内核 GET /logs/download?filename=...）。
     * 先查询后下载模式：服务层先调 listKernelLogFiles() 获取文件列表，
     * 用户选择具体文件后，调此方法按 filename 下载该文件。
     * <p>
     * 流式返回：返回上游原始 ClientHttpResponse，其响应体 InputStream 保持打开，
     * 由调用方负责读取并在完成后 close（控制器 StreamingResponseBody / zip 打包），
     * 全程不经过 byte[] 堆缓冲。
     *
     * @param filename 日志文件相对路径（由 /logs/files 返回的 filename 字段）
     * @return 上游原始响应（调用方负责 close；响应头含 Content-Length 等透传信息）
     */
    default org.springframework.http.client.ClientHttpResponse downloadKernelLogs(String filename) {
        throw new GapException("内核日志下载 API 未暴露，待记忆服务补 GET /logs/download?filename=...");
    }

    /**
     * 列出内核日志目录下所有可下载的日志文件（内核 GET /logs/files）。
     * 先查询后下载模式：服务层先调用此方法获取真实文件列表，再决定下载哪些。
     *
     * @return 文件信息列表，每项含 filename/log_type/size_bytes/size_human/modified_at/is_rotated
     */
    default List<Map<String, Object>> listKernelLogFiles() {
        throw new GapException("内核日志文件列表 API 未暴露，待记忆服务补 GET /logs/files");
    }

    // —— 用户消息日志（V3 §6.6.4 KR-MSG-01~04，数据源=内核 user_message 表，不经服务层落库）——

    /**
     * KR-MSG-01：分页查询用户消息日志（内核 POST /admin/messages/query）。
     *
     * @param filter 过滤条件，可含 scope_id/user_id/session_id/start_time/end_time（ISO-8601）/page_idx/page_size
     * @return 内核原始响应 {total, page_idx, page_size, items:[{message_id,user_id,scope_id,session_id,role,content,timestamp}]}
     */
    default Map<String, Object> queryKernelMessages(Map<String, Object> filter) {
        throw new GapException("用户消息查询 API 未由 :8516 暴露，待记忆服务补 POST /admin/messages/query");
    }

    /**
     * KR-MSG-02：用户消息统计（内核 GET /admin/messages/stats）。
     *
     * @return 内核原始响应 {total, by_role:{role:count}}
     */
    default Map<String, Object> statsKernelMessages(String scopeId, String userId, String sessionId,
                                                    String startTime, String endTime) {
        throw new GapException("用户消息统计 API 未由 :8516 暴露，待记忆服务补 GET /admin/messages/stats");
    }

    /**
     * KR-MSG-03：导出用户消息 CSV（内核 GET /admin/messages/export）。
     *
     * @return CSV 文件字节
     */
    default byte[] exportKernelMessages(String scopeId, String userId, String sessionId,
                                        String startTime, String endTime, int limit) {
        throw new GapException("用户消息导出 API 未由 :8516 暴露，待记忆服务补 GET /admin/messages/export");
    }

    /**
     * KR-MSG-04：单条用户消息详情（内核 GET /admin/messages/detail/{msgId}）。
     *
     * @return 内核原始响应（完整元数据），消息不存在时返回 null
     */
    default Map<String, Object> getKernelMessageDetail(String msgId) {
        throw new GapException("用户消息详情 API 未由 :8516 暴露，待记忆服务补 GET /admin/messages/detail/{msgId}");
    }

    // —— V3-DEFECT-058/059 修复：新增内核调用方法 ——
    
    /**
     * V3-DEFECT-058: 获取记忆完整元数据
     * 调用 Python 内核的 message_manager.get_with_metadata() 或对应方法
     * @return 内核原始响应 {found, mem_id, content, type, timestamp, fields{...}}
     */
    default Map<String, Object> getMemoryWithMetadata(String userId, String scopeId, String memId) {
        throw new GapException("get_memory_with_metadata 未由 :8516 暴露，待记忆服务补内核实现");
    }
    
    /**
     * V3-DEFECT-059: 按角色统计用户消息数量
     * 调用 Python 内核的 sql_message_store.count_by_role() 方法
     * @return 内核原始响应 {by_role:{user:N,assistant:N}, total:N}
     */
    default Map<String, Object> countMessagesByRole(String userId, String scopeId, String sessionId) {
        throw new GapException("count_messages_by_role 未由 :8516 暴露，待记忆服务补内核实现");
    }

    default Object startDreaming(Object config) {
        throw new GapException("start_dreaming 未由 :8516 暴露，待记忆服务补 /ops/dreaming/start");
    }

    default Object stopDreaming(String scopeId, String userId) {
        throw new GapException("stop_dreaming 未由 :8516 暴露，待记忆服务补 /ops/dreaming/stop");
    }

    default Object dreamingStatus() {
        throw new GapException("dreaming_status 未由 :8516 暴露，待记忆服务补 /ops/dreaming/status");
    }

    default Object migrate(Map<String, Object> source, Map<String, Object> target) {
        throw new GapException("向量迁移 API 未由 :8516 暴露，待记忆服务补 /ops/migration");
    }
}
