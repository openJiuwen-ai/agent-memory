package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.common.client.dto.AddMessagesRequest;
import com.openjiuwen.memory.opscenter.domain.FeatureFlagEntity;

import java.util.List;

/**
 * 特性配置服务（功能3）。
 * <p>
 * :8516 的 5 个 enable_* 仅作为 /add_messages/ 调用参数，无持久化接口。
 * 本服务在本地 DB 持久化特性 profile（按 scope 维度），并在新增记忆时注入到 AddMessagesRequest。
 */
public interface FeatureFlagService {

    /** 全部特性 profile 列表（按 tenant）。 */
    List<FeatureFlagEntity> list();

    /**
     * 解析某 scope 最终生效的 enable_* 五元组（scope 级覆盖默认级，未设项回落默认）。
     * 供 MemoryManageService 新增记忆时注入。
     */
    AddMessagesRequest resolve(String scopeId);

    /** 查询某 scope 的原始 profile + 合并后结果 */
    FeatureView get(String scopeId);

    /** upsert scope 级 profile */
    void upsert(String scopeId, AddMessagesRequest flags, String operator);

    /** 快捷切换单个 enable_* */
    void toggle(String scopeId, String flag, boolean value, String operator);

    /** 删除 scope 级 profile（回退默认），__default__ 禁止删除 */
    void delete(String scopeId, String operator);

    record FeatureView(AddMessagesRequest profile, AddMessagesRequest resolved, String inheritedFrom) {
    }
}
