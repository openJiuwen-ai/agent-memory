package com.openjiuwen.memory.configcenter.service;

import com.openjiuwen.memory.configcenter.dto.KernelConfigUpdateRequest;
import com.openjiuwen.memory.configcenter.dto.KernelConfigUpdateResultDTO;

import java.util.Map;

/**
 * 内核配置管理服务 — Push 模型。
 * <p>
 * 遵循设计文档 §5.3 Push 模型：
 * <ul>
 *   <li>GET：代理调用内核 GET /admin/config，敏感字段脱敏展示</li>
 *   <li>PUT：过滤只读参数 → 调用内核 PUT /admin/config 写入 → 调用 POST /admin/restart 重启</li>
 * </ul>
 * <p>
 * 核心原则：内核始终是唯一配置源，服务层是内核的"远程编辑器"，不是配置源。
 * <p>
 * 2026-07-19 P0-3 v3：内核配置页只读展示安装参数 + 连接参数。
 * 可修改参数拆分为热启动模板（tpl_instance_hot，立即生效）与冷启动模板
 * （tpl_instance_cold，需重启），由 ConfigTemplateService 管理。
 */
public interface KernelConfigService {

    /**
     * 获取内核当前配置（只读，敏感字段脱敏）。
     * <p>
     * 代理调用内核 GET /admin/config，每个参数附带 editable / category / danger 标记。
     * 返回结构按分类组织：runtime / storage / vector_engine / engine。
     *
     * @return 内核配置 Map
     */
    Map<String, Object> getKernelConfig();

    /**
     * 修改内核可热生效参数（Push 模型）。
     * <p>
     * 仅用于 Push 可热生效参数；安装参数与连接参数为只读，拒绝修改。
     * 可修改参数请到配置模板的热启动/冷启动模板中编辑。
     * <p>
     * 流程：
     * <ol>
     *   <li>过滤只读参数（安装参数 + 连接参数，拒绝修改）</li>
     *   <li>调用内核 PUT /admin/config 写入</li>
     *   <li>调用内核 POST /admin/restart 触发重启（若 restart=true）</li>
     *   <li>记录审计日志</li>
     * </ol>
     *
     * @param request  更新请求（updates + restart + reason）
     * @param operator 操作人
     * @return 更新结果（updated_keys / rejected_keys / restart_triggered / restart_status）
     */
    KernelConfigUpdateResultDTO updateKernelConfig(KernelConfigUpdateRequest request, String operator);
}
