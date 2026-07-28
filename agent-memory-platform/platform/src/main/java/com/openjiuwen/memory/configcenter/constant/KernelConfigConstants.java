package com.openjiuwen.memory.configcenter.constant;

import java.util.Set;

/**
 * 内核配置只读参数常量 — P2-2 整改：提取为共享常量。
 * <p>
 * 这些参数在内核配置页/实例配置页只展示，拒绝通过 Push 修改。
 * 包含两类：
 * <ul>
 *   <li>安装参数：部署时确定，平台不可改（IP/PORT/数据目录/存储类型/密钥）</li>
 *   <li>连接参数：LLM/Embedding 连接的全局默认值，scope 未配置时兜底，
 *       由 SCOPE 模板覆盖；此处只读展示，不通过 Push 修改</li>
 * </ul>
 * 参考 §5.3.4 ARCHITECTURE_PARAMS。
 * <p>
 * KernelConfigServiceImpl 和 InstanceConfigServiceImpl 共用此常量，
 * 确保所有 Push 到内核的路径都过滤只读参数。
 */
public final class KernelConfigConstants {

    private KernelConfigConstants() {
        // 工具类，禁止实例化
    }

    /**
     * 只读参数白名单 — 这些参数拒绝通过 Push 修改。
     */
    public static final Set<String> READONLY_PARAMS = Set.of(
            // 架构参数（V3 §5.2 — 部署时确定，平台不可改）
            "ARCHITECTURE_TYPE",
            // 安装参数
            "KV_STORE_TYPE", "DB_STORE_TYPE", "VECTOR_STORE_TYPE",
            "MEMORY_DATA_DIR", "CRYPTO_KEY", "IP", "PORT", "MEMORY_API_KEY",
            // 连接参数（全局默认值，scope 可覆盖）
            "MODEL_PROVIDER", "API_BASE", "API_KEY", "MODEL_NAME",
            "EMBED_MODEL_NAME", "EMBED_API_BASE", "EMBED_API_KEY"
    );

    /**
     * 判断给定参数名是否为只读参数（大小写不敏感）。
     *
     * @param paramName 参数名
     * @return true 表示该参数只读，不可通过 Push 修改
     */
    public static boolean isReadonly(String paramName) {
        if (paramName == null || paramName.isBlank()) {
            return false;
        }
        return READONLY_PARAMS.contains(paramName.toUpperCase());
    }
}
