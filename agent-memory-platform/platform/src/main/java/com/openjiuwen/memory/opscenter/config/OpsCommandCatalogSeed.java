package com.openjiuwen.memory.opscenter.config;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.openjiuwen.memory.opscenter.domain.OpsCommandCatalogEntity;
import com.openjiuwen.memory.opscenter.mapper.OpsCommandCatalogMapper;
import org.springframework.boot.CommandLineRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 命令目录种子（功能1）。运维命令 = 对系统本身的管理操作；业务接口不在本目录。
 * 幂等：不存在则插入，已存在则更新可变字段（enabled/gapReason/backendAction 等），
 * 让 seed 代码变更（如 :8516 补齐端点后把 enabled 从 false 改 true）能对已存在的记录生效。
 */
@Configuration
public class OpsCommandCatalogSeed {

    @Bean
    public CommandLineRunner seedCommands(OpsCommandCatalogMapper mapper) {
        return args -> seed(mapper);
    }

    private void seed(OpsCommandCatalogMapper mapper) {
        // 清理已移除的命令记录（本期不做的命令：clear_cache/rebuild_index/dreaming 三件套走任务管理/migrate 归 F9）
        mapper.delete(new LambdaQueryWrapper<OpsCommandCatalogEntity>().in(
                OpsCommandCatalogEntity::getCommandCode,
                java.util.List.of("CLEAR_CACHE", "REBUILD_INDEX", "START_DREAMING", "STOP_DREAMING", "DREAMING_STATUS", "MIGRATE_VECTOR")));

        insert(mapper, "HEALTH_INSPECTION", "健康巡检", "inspection", "client.health",
                true, null, false, "调用 /health 探测引擎运行状态（浅状态）");
        insert(mapper, "RESTART_KERNEL", "重启内核", "admin", "client.restartKernel",
                true, null, true, "触发内核热重建（:8516 /admin/restart，不杀进程，重读 .env + 重建 stores）");
        insert(mapper, "RELOAD_CONFIG", "配置热加载", "admin", "client.reloadConfig",
                true, null, false, "热加载引擎运行时配置（:8516 /admin/reload-config，重读 .env + 重建 stores，不杀进程）");
        // 以下命令本期不做，已从目录移除（重启后 cleanup 自动删除 DB 旧记录）：
        // - CLEAR_CACHE / REBUILD_INDEX：:8516 内核无现成方法，待后续
        // - START_DREAMING / STOP_DREAMING / DREAMING_STATUS：走「任务管理」Dreaming 面板，不进命令目录
        // - MIGRATE_VECTOR：F9 数据迁移不在本期
        // insert(mapper, "CLEAR_CACHE", "清理缓存", "maintenance", "client.clearCache",
        //         false, ":8516 未暴露，待记忆服务补 /admin/clear-cache", true, "清理引擎运行时缓存");
        // insert(mapper, "REBUILD_INDEX", "重建向量索引", "maintenance", "client.rebuildIndex",
        //         false, ":8516 未暴露，待记忆服务补 /admin/rebuild-index", true, "重建向量索引");
        // insert(mapper, "START_DREAMING", "启动 Dreaming", "task", "client.startDreaming",
        //         false, ":8516 未暴露，待记忆服务补 /ops/dreaming/start", true, "启动跨会话知识巩固");
        // insert(mapper, "STOP_DREAMING", "停止 Dreaming", "task", "client.stopDreaming",
        //         false, ":8516 未暴露，待记忆服务补 /ops/dreaming/stop", true, "停止 Dreaming");
        // insert(mapper, "DREAMING_STATUS", "Dreaming 状态", "task", "client.dreamingStatus",
        //         false, ":8516 未暴露，待记忆服务补 /ops/dreaming/status", false, "查询 Dreaming 运行状态");
        // insert(mapper, "MIGRATE_VECTOR", "向量索引迁移", "maintenance", "client.migrate",
        //         false, ":8516 未暴露，待记忆服务补 /ops/migration", true, "在两个向量索引间批量迁移数据");
    }

    private void insert(OpsCommandCatalogMapper mapper, String code, String name, String category,
                        String backendAction, boolean enabled, String gapReason,
                        boolean requireConfirm, String description) {
        if (mapper.selectById(code) != null) {
            // 已存在则用 UpdateWrapper 显式更新所有可变字段（含 gapReason=null 清空旧提示），
            // 避免 updateById 的 NOT_NULL 策略跳过 null 字段。
            LambdaUpdateWrapper<OpsCommandCatalogEntity> w = new LambdaUpdateWrapper<>();
            w.eq(OpsCommandCatalogEntity::getCommandCode, code)
                    .set(OpsCommandCatalogEntity::getCommandName, name)
                    .set(OpsCommandCatalogEntity::getCategory, category)
                    .set(OpsCommandCatalogEntity::getBackendAction, backendAction)
                    .set(OpsCommandCatalogEntity::getEnabled, enabled)
                    .set(OpsCommandCatalogEntity::getGapReason, gapReason)
                    .set(OpsCommandCatalogEntity::getRequireConfirm, requireConfirm)
                    .set(OpsCommandCatalogEntity::getDescription, description);
            mapper.update(null, w);
            return;
        }
        OpsCommandCatalogEntity e = new OpsCommandCatalogEntity();
        e.setCommandCode(code);
        e.setCommandName(name);
        e.setCategory(category);
        e.setBackendAction(backendAction);
        e.setEnabled(enabled);
        e.setGapReason(gapReason);
        e.setRequireConfirm(requireConfirm);
        e.setDescription(description);
        mapper.insert(e);
    }
}
