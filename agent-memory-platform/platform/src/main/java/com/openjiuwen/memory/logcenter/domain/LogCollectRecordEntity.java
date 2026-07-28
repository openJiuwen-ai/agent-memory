package com.openjiuwen.memory.logcenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import com.openjiuwen.memory.logcenter.handler.InstantTextTypeHandler;

import java.time.Instant;

/**
 * 日志一键采集记录（§6.4.4）。
 * 命名规则：场景-时间戳-UUID
 */
@TableName(value = "log_collect_records", autoResultMap = true)
public class LogCollectRecordEntity {

    /**
     * 主键 id：Controller 生成 8 位 hex UUID 后 set 进来，必须用 INPUT，
     * 否则全局 application.yml 的 id-type: auto 会让 MP 用 SQLite rowid 回写覆盖，
     * 导致 Java 对象里的 id 变成数字（1/2/...），与 DB 实际存储的 UUID 不一致，
     * 进而 selectById/updateById 全部失配（采集状态卡 COLLECTING + 轮询 404）。
     */
    @TableId(type = IdType.INPUT)
    private String id;

    private String scene;

    private String name;

    private String startDate;

    private String endDate;

    private String tenantId;

    private String filePath;

    private Long fileSize;

    private String status;

    private String operatorId;

    /** SQLite 中 created_at 为 TEXT 列，需用字符串方式读写，避免 JDBC getTimestamp 解析 ISO 串失败 */
    @TableField(typeHandler = InstantTextTypeHandler.class)
    private Instant createdAt;

    private String remark;

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }

    public String getScene() { return scene; }
    public void setScene(String scene) { this.scene = scene; }

    public String getName() { return name; }
    public void setName(String name) { this.name = name; }

    public String getStartDate() { return startDate; }
    public void setStartDate(String startDate) { this.startDate = startDate; }

    public String getEndDate() { return endDate; }
    public void setEndDate(String endDate) { this.endDate = endDate; }

    public String getTenantId() { return tenantId; }
    public void setTenantId(String tenantId) { this.tenantId = tenantId; }

    public String getFilePath() { return filePath; }
    public void setFilePath(String filePath) { this.filePath = filePath; }

    public Long getFileSize() { return fileSize; }
    public void setFileSize(Long fileSize) { this.fileSize = fileSize; }

    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }

    public String getOperatorId() { return operatorId; }
    public void setOperatorId(String operatorId) { this.operatorId = operatorId; }

    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }

    public String getRemark() { return remark; }
    public void setRemark(String remark) { this.remark = remark; }
}
