package com.openjiuwen.memory.common.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

import java.util.concurrent.ThreadPoolExecutor;

/**
 * 异步任务线程池配置。
 * <p>
 * Fix #4/#15: 为审计日志等 I/O 密集型异步任务提供专用线程池，
 * 避免使用 ForkJoinPool.commonPool() 导致全局并行计算饥饿。
 * <p>
 * 背压策略：队列满时由调用线程同步执行（CallerRunsPolicy），
 * 自然限流，防止 OOM。
 */
@Configuration
public class AsyncThreadPoolConfig {

    /**
     * 审计日志专用线程池。
     * - 核心线程 2，最大线程 4，队列容量 256
     * - 队列满时调用线程同步执行（背压）
     */
    @Bean(name = "auditLogExecutor")
    public ThreadPoolTaskExecutor auditLogExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(2);
        executor.setMaxPoolSize(4);
        executor.setQueueCapacity(256);
        executor.setThreadNamePrefix("audit-log-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.CallerRunsPolicy());
        executor.setWaitForTasksToCompleteOnShutdown(true);
        executor.setAwaitTerminationSeconds(10);
        executor.initialize();
        return executor;
    }

    /**
     * 日志采集专用线程池。
     * - 核心线程 1，最大线程 2，队列容量 5
     * - 日志采集是 CPU/IO 密集型任务，需要较大队列缓冲
     * - 队列满时调用线程同步执行（背压）
     */
    @Bean(name = "logCollectExecutor")
    public ThreadPoolTaskExecutor logCollectExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(1);
        executor.setMaxPoolSize(2);
        executor.setQueueCapacity(5);
        executor.setThreadNamePrefix("log-collect-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.CallerRunsPolicy());
        executor.setWaitForTasksToCompleteOnShutdown(true);
        executor.setAwaitTerminationSeconds(60);
        executor.initialize();
        return executor;
    }
}
