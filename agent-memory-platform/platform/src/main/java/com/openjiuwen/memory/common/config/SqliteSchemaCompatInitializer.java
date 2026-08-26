package com.openjiuwen.memory.common.config;

import org.springframework.beans.BeansException;
import org.springframework.beans.factory.config.BeanFactoryPostProcessor;
import org.springframework.beans.factory.config.ConfigurableListableBeanFactory;
import org.springframework.context.EnvironmentAware;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

/**
 * 在 Spring SQL 初始化前修补旧版 SQLite 库结构，保证 IF NOT EXISTS 建表脚本可重复执行。
 * 主要兼容旧版配置中心库表与当前 V3 结构的列差异，避免初始化索引或后续插入审计日志时失败。
 */
@Component
public class SqliteSchemaCompatInitializer implements BeanFactoryPostProcessor, EnvironmentAware {

    private Environment environment;

    @Override
    public void setEnvironment(Environment environment) {
        this.environment = environment;
    }

    @Override
    public void postProcessBeanFactory(ConfigurableListableBeanFactory beanFactory) throws BeansException {
        String url = environment.getProperty("spring.datasource.url");
        if (url == null || !url.startsWith("jdbc:sqlite:")) {
            return;
        }
        try (Connection connection = DriverManager.getConnection(url)) {
            compatTenants(connection);
            compatUsers(connection);
            compatConfigTemplates(connection);
            compatConfigAuditLogs(connection);
        } catch (Exception e) {
            throw new IllegalStateException("SQLite 兼容初始化失败: " + e.getMessage(), e);
        }
    }

    private void compatTenants(Connection connection) throws Exception {
        if (!tableExists(connection, "tenants")) {
            return;
        }
        addColumnIfMissing(connection, "tenants", "scope_ids", "TEXT", null);
    }

    private void compatUsers(Connection connection) throws Exception {
        if (!tableExists(connection, "users")) {
            return;
        }
        addColumnIfMissing(connection, "users", "scope_ids", "TEXT", null);
    }

    private void compatConfigTemplates(Connection connection) throws Exception {
        if (!tableExists(connection, "config_templates")) {
            return;
        }
        addColumnIfMissing(connection, "config_templates", "parent_id", "TEXT", null);
        try (Statement statement = connection.createStatement()) {
            statement.execute("CREATE INDEX IF NOT EXISTS idx_template_parent ON config_templates(parent_id)");
        }
    }

    private void compatConfigAuditLogs(Connection connection) throws Exception {
        if (!tableExists(connection, "config_audit_logs")) {
            return;
        }
        addColumnIfMissing(connection, "config_audit_logs", "operator_id", "VARCHAR(64)", "admin_user_id");
        addColumnIfMissing(connection, "config_audit_logs", "tenant_id", "TEXT", null);
        addColumnIfMissing(connection, "config_audit_logs", "template_id", "TEXT", "resource_id");
        addColumnIfMissing(connection, "config_audit_logs", "instance_id", "VARCHAR(64) NOT NULL DEFAULT 'default'", null);
        addColumnIfMissing(connection, "config_audit_logs", "before_value", "TEXT", "before_config");
        addColumnIfMissing(connection, "config_audit_logs", "after_value", "TEXT", "after_config");
        addColumnIfMissing(connection, "config_audit_logs", "reason", "TEXT", null);
        try (Statement statement = connection.createStatement()) {
            statement.execute("CREATE INDEX IF NOT EXISTS idx_cfg_audit_tenant_time ON config_audit_logs(tenant_id, operated_at)");
            statement.execute("CREATE INDEX IF NOT EXISTS idx_cfg_audit_template_time ON config_audit_logs(template_id, operated_at)");
            statement.execute("CREATE INDEX IF NOT EXISTS idx_cfg_audit_operator_time ON config_audit_logs(operator_id, operated_at)");
            statement.execute("CREATE INDEX IF NOT EXISTS idx_cfg_audit_instance ON config_audit_logs(instance_id)");
        }
    }

    private boolean tableExists(Connection connection, String tableName) throws Exception {
        DatabaseMetaData metaData = connection.getMetaData();
        try (ResultSet rs = metaData.getTables(null, null, tableName, new String[]{"TABLE"})) {
            return rs.next();
        }
    }

    private boolean columnExists(Connection connection, String tableName, String columnName) throws Exception {
        DatabaseMetaData metaData = connection.getMetaData();
        try (ResultSet rs = metaData.getColumns(null, null, tableName, columnName)) {
            return rs.next();
        }
    }

    private static final java.util.regex.Pattern IDENT_PATTERN =
            java.util.regex.Pattern.compile("^[a-zA-Z_][a-zA-Z0-9_]*$");

    private void validateIdentifier(String name) {
        if (name == null || !IDENT_PATTERN.matcher(name).matches()) {
            throw new IllegalArgumentException("Invalid SQL identifier: " + name);
        }
    }

    private void addColumnIfMissing(Connection connection,
                                    String tableName,
                                    String columnName,
                                    String columnDefinition,
                                    String copyFromColumn) throws Exception {
        validateIdentifier(tableName);
        validateIdentifier(columnName);
        if (copyFromColumn != null) {
            validateIdentifier(copyFromColumn);
        }
        if (columnExists(connection, tableName, columnName)) {
            return;
        }
        try (Statement statement = connection.createStatement()) {
            statement.execute("ALTER TABLE " + tableName + " ADD COLUMN " + columnName + " " + columnDefinition);
            if (copyFromColumn != null && columnExists(connection, tableName, copyFromColumn)) {
                statement.executeUpdate("UPDATE " + tableName
                        + " SET " + columnName + " = " + copyFromColumn
                        + " WHERE " + columnName + " IS NULL");
            }
        }
    }
}
