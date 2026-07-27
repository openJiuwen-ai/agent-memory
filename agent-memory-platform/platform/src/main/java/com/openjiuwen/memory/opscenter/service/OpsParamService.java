package com.openjiuwen.memory.opscenter.service;

import java.util.Map;

/** 运维参数配置（功能2）：系统自身全局参数（检索/引擎/Scope/Agent/Dreaming）。 */
public interface OpsParamService {

    /** 参数分类总览（available/hasDraft） */
    Map<String, Object> overview();

    /** 取某分类参数：available(配置中心) + draft(本地) + effective(本模块调用 :8516 用的默认值) */
    Map<String, Object> get(String category, String scopeId);

    /** 写回内核经配置中心 SPI；未接入则仅存本地草稿并返回 gap */
    Map<String, Object> update(String category, String scopeId, Map<String, Object> value, String operator);

    /** 仅存本地草稿，不写回内核 */
    void saveDraft(String category, String scopeId, Map<String, Object> value, String operator);
}
