package com.openjiuwen.memory.opscenter.domain;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

@Data
@TableName("ops_command_catalog")
public class OpsCommandCatalogEntity {

    /** 命令编码主键，由种子提供（非自增） */
    @TableId(type = IdType.INPUT)
    private String commandCode;
    private String commandName;
    private String category;
    private String backendAction;
    private Boolean enabled = true;
    private String gapReason;
    private Boolean requireConfirm = false;
    private String description;
}
