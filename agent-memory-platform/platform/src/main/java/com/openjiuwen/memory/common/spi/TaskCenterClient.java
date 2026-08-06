package com.openjiuwen.memory.common.spi;

/**
 * 异步任务调度（属"任务中心"模块）。清理/扫描/迁移/大批量导出等耗时任务经此提交。
 */
public interface TaskCenterClient {

    /** 提交异步任务；缺省实现 fallback 为同步执行并告警。 */
    String submit(TaskDefinition def);

    record TaskDefinition(String type, String scopeId, String userId, java.util.Map<String, Object> payload) {
    }
}
