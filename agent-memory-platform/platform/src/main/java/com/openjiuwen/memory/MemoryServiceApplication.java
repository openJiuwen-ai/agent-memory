package com.openjiuwen.memory;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 记忆服务平台启动入口（模块化单体）。
 * <p>
 * 包路径 {@code com.openjiuwen.memory} 下按业务模块划分目录，便于多人并行开发与后续模块扩展：
 * <pre>
 *   com.openjiuwen.memory
 *   ├── common         共享层：统一响应/异常/SPI 接口/记忆服务 Client/全局配置
 *   ├── opscenter      运维中心模块（当前实现）
 *   ├── configcenter   配置中心（占位，待开发）
 *   ├── logcenter      日志中心（占位）
 *   ├── alertcenter    告警中心（占位）
 *   ├── monitoring     监控巡检（占位）
 *   ├── installation   安装升级（占位）
 *   └── taskcenter     任务中心（占位）
 * </pre>
 * 新增模块只需在 {@code com.openjiuwen.memory} 下建包并放 Controller/Service/Mapper，
 * 本类的 {@code @SpringBootApplication}（默认扫描主类所在包及子包）会自动发现；
 * Mapper 需在下方 {@code @MapperScan} 追加该模块的 mapper 包。
 */
@SpringBootApplication
@EnableScheduling
@EnableAsync
public class MemoryServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(MemoryServiceApplication.class, args);
    }
}
