package com.openjiuwen.memory.common.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.BeansException;
import org.springframework.beans.factory.config.BeanFactoryPostProcessor;
import org.springframework.beans.factory.config.ConfigurableListableBeanFactory;
import org.springframework.stereotype.Component;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * 启动早期（早于 DataSource/Hikari 初始化）确保 SQLite 库文件的父目录存在。
 *
 * <p>背景：SQLite JDBC 驱动能自动创建 db 文件，但不会创建其父目录。
 * 当工作目录下没有 data/ 时（如 IDEA 默认工作目录=仓库根，而非 platform/），
 * HikariCP fail-fast 会导致 Spring 上下文启动失败。
 *
 * <p>本类实现 {@link BeanFactoryPostProcessor}，在 Bean 定义注册阶段
 * （早于任何单例实例化）从 JDBC URL 解析出文件路径并创建父目录，
 * 兼容任意工作目录启动场景。
 *
 * <p>注意：BeanFactoryPostProcessor 的实例化发生在 registerBeanPostProcessors 之前，
 * 此时负责解析 {@code @Value}/{@code @Autowired} 的 AutowiredAnnotationBeanPostProcessor
 * 尚未注册，因此本类不能使用 {@code @Value} 构造器注入（否则会因找不到默认构造器抛出
 * NoSuchMethodException）。数据源 URL 改在 {@link #postProcessBeanFactory} 内通过
 * {@link ConfigurableListableBeanFactory#resolveEmbeddedValue} 解析占位符——其依赖的
 * EmbeddedValueResolver 在 prepareBeanFactory 阶段已就绪，早于本方法调用。
 */
@Component
public class DataDirInitializer implements BeanFactoryPostProcessor {

    private static final Logger log = LoggerFactory.getLogger(DataDirInitializer.class);
    private static final String SQLITE_URL_PREFIX = "jdbc:sqlite:";
    private static final String IN_MEMORY_DB = ":memory:";
    private static final String DATASOURCE_URL_PLACEHOLDER = "${spring.datasource.url:}";

    @Override
    public void postProcessBeanFactory(ConfigurableListableBeanFactory beanFactory) throws BeansException {
        // BeanFactoryPostProcessor 实例化早于 BeanPostProcessor 注册，无法用 @Value 注入，
        // 在此通过 beanFactory.resolveEmbeddedValue 解析占位符。
        String datasourceUrl = beanFactory.resolveEmbeddedValue(DATASOURCE_URL_PLACEHOLDER);
        String dbPath = extractSqliteFilePath(datasourceUrl);
        if (dbPath == null) {
            return;
        }
        ensureParentDirExists(dbPath);
    }

    /**
     * 从 JDBC URL 中提取 SQLite 文件路径。
     * 仅处理 {@code jdbc:sqlite:} 前缀的 URL，忽略内存数据库和空路径。
     *
     * @param url JDBC 连接串
     * @return 数据库文件路径，非 SQLite 或内存库时返回 null
     */
    private String extractSqliteFilePath(String url) {
        if (url == null || !url.startsWith(SQLITE_URL_PREFIX)) {
            return null;
        }
        String path = url.substring(SQLITE_URL_PREFIX.length());
        int queryIndex = path.indexOf('?');
        if (queryIndex >= 0) {
            path = path.substring(0, queryIndex);
        }
        if (path.isBlank() || IN_MEMORY_DB.equals(path)) {
            return null;
        }
        return path;
    }

    /**
     * 确保数据库文件的父目录存在，不存在则递归创建。
     * 创建失败不阻断启动，交由 HikariCP 给出原始错误。
     *
     * @param dbPath 数据库文件路径
     */
    private void ensureParentDirExists(String dbPath) {
        Path parent = Paths.get(dbPath).toAbsolutePath().getParent();
        if (parent == null || Files.exists(parent)) {
            return;
        }
        try {
            Files.createDirectories(parent);
            log.info("Created SQLite data directory: {}", parent);
        } catch (Exception e) {
            log.warn("Failed to create SQLite data directory: {}", parent, e);
        }
    }
}
