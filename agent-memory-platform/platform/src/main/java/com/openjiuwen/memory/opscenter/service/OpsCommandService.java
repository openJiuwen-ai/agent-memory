package com.openjiuwen.memory.opscenter.service;

import com.openjiuwen.memory.common.PageResult;
import com.openjiuwen.memory.opscenter.domain.CommandExecutionLogEntity;
import com.openjiuwen.memory.opscenter.domain.OpsCommandCatalogEntity;

import java.util.List;
import java.util.Map;

/** 远程运维命令（功能1）。 */
public interface OpsCommandService {

    /** 命令目录（含 enabled/gap 标记） */
    List<OpsCommandCatalogEntity> catalog(String category);

    /** 下发命令：enabled→路由调用；gap→返回 gap；dryRun→回显不调用 */
    Map<String, Object> dispatch(String commandCode, String scopeId, String userId,
                                 Map<String, Object> payload, boolean dryRun, String reason, String operator);

    /** 查询执行结果 */
    CommandExecutionLogEntity execution(String executionId);

    /** 执行历史列表（按 created_at desc，可选 command_code/status 过滤） */
    PageResult<CommandExecutionLogEntity> executions(int pageIdx, int pageSize, String commandCode, String status);
}
