package com.openjiuwen.memory.tenantcenter.controller;


import com.openjiuwen.memory.authcenter.service.UserService;
import com.openjiuwen.memory.common.CommonResult;
import com.openjiuwen.memory.configcenter.domain.ConfigTemplateEntity;
import com.openjiuwen.memory.configcenter.domain.TenantScopeConfigEntity;
import com.openjiuwen.memory.configcenter.mapper.ConfigTemplateMapper;
import com.openjiuwen.memory.configcenter.mapper.TenantScopeConfigMapper;
import com.openjiuwen.memory.scopecenter.domain.ScopeRegistry;
import com.openjiuwen.memory.scopecenter.service.ScopeRegistryService;
import com.openjiuwen.memory.tenantcenter.domain.Tenant;
import com.openjiuwen.memory.tenantcenter.service.TenantService;
import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.*;
import org.springframework.transaction.annotation.Transactional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Collectors;

/**
 * 租户管理 REST API
 */
@RestController
@RequestMapping("/api/v1/tenants")
public class TenantController {

    private static final Logger log = LoggerFactory.getLogger(TenantController.class);
    
    @Autowired
    private TenantService tenantService;

    @Autowired
    private TenantScopeConfigMapper tenantScopeConfigMapper;

    @Autowired
    private ConfigTemplateMapper configTemplateMapper;
    
    @Autowired
    private ScopeRegistryService scopeRegistryService;
    
    @Autowired
    private UserService userService;
    
    private final ObjectMapper objectMapper = new ObjectMapper();
    
    /**
     * 获取租户列表
     */
    @GetMapping
    public CommonResult<List<Tenant>> list() {
        List<Tenant> tenants = tenantService.list();
        if (!tenants.isEmpty()) {
            List<TenantScopeConfigEntity> scopeConfigs = tenantScopeConfigMapper.selectList(null);
            Map<String, String> tenantIdToTemplateId = scopeConfigs.stream()
                .collect(Collectors.toMap(TenantScopeConfigEntity::getTenantId,
                    TenantScopeConfigEntity::getTemplateId, (a, b) -> a));
            List<ConfigTemplateEntity> templates = configTemplateMapper.selectList(null);
            Map<String, String> templateIdToName = templates.stream()
                .collect(Collectors.toMap(ConfigTemplateEntity::getId,
                    ConfigTemplateEntity::getTemplateName, (a, b) -> a));
            for (Tenant tenant : tenants) {
                String templateId = tenantIdToTemplateId.get(tenant.getId());
                if (templateId != null) {
                    tenant.setCurrentTemplateId(templateId);
                    tenant.setCurrentTemplateName(templateIdToName.getOrDefault(templateId, templateId));
                }
            }
        }
        return CommonResult.success(tenants);
    }
    
    /**
     * 获取租户详情
     */
    @GetMapping("/{tenantId}")
    public CommonResult<Tenant> getById(@PathVariable String tenantId) {
        Tenant tenant = tenantService.getById(tenantId);
        if (tenant == null) {
            return CommonResult.error("租户不存在");
        }
        return CommonResult.success(tenant);
    }
    
    /**
     * 创建租户
     */
    @PostMapping
    @Transactional(rollbackFor = Exception.class)
    public CommonResult<Tenant> create(@RequestBody Map<String, Object> request) {
        try {
            Tenant tenant = new Tenant();
            tenant.setId(UUID.randomUUID().toString().replace("-", ""));
            tenant.setName((String) request.get("name"));
            tenant.setRemark((String) request.get("remark"));
            tenant.setCreatedAt(LocalDateTime.now());
            tenant.setUpdatedAt(LocalDateTime.now());
            tenant.setStatus("active");
            
            // 处理scope分配
            @SuppressWarnings("unchecked")
            List<String> scopeIds = (List<String>) request.get("scopeIds");
            if (scopeIds != null && scopeIds.size() > 1) {
                return CommonResult.error("当前设计只允许一个租户绑定一个 scope_id");
            }
            if (scopeIds != null && !scopeIds.isEmpty()) {
                // 将scopeIds转换为JSON字符串存储
                tenant.setScopeIds(objectMapper.writeValueAsString(scopeIds));
            }
            
            // 先保存租户（生成tenant ID）
            boolean success = tenantService.save(tenant);
            if (!success) {
                return CommonResult.error("创建租户失败");
            }

            // 租户保存成功后，再分配scope。
            // 关键：assign 失败必须重抛异常触发事务回滚，不能吞掉只返回 error 字符串——
            // 否则 @Transactional 因异常被捕获而不回滚，导致 tenant 落库但 scope_registry 没写入（孤儿数据）。
            if (scopeIds != null && !scopeIds.isEmpty()) {
                try {
                    scopeRegistryService.batchAssignScopesToTenant(scopeIds, tenant.getId());
                } catch (Exception assignEx) {
                    throw new RuntimeException("Scope 绑定失败，租户创建已回滚：" + assignEx.getMessage(), assignEx);
                }
            }

            return CommonResult.success(tenant);
        } catch (Exception e) {
            return CommonResult.error("创建租户失败：" + e.getMessage());
        }
    }
    
    /**
     * 更新租户
     */
    @PutMapping("/{tenantId}")
    @Transactional(rollbackFor = Exception.class)
    public CommonResult<Tenant> update(@PathVariable String tenantId, @RequestBody Map<String, Object> request) {
        try {
            Tenant existingTenant = tenantService.getById(tenantId);
            if (existingTenant == null) {
                return CommonResult.error("租户不存在");
            }
            
            // 更新基本信息
            existingTenant.setName((String) request.get("name"));
            existingTenant.setRemark((String) request.get("remark"));
            existingTenant.setStatus((String) request.get("status"));
            existingTenant.setUpdatedAt(LocalDateTime.now());
            
            // 处理scope变更
            @SuppressWarnings("unchecked")
            List<String> newScopeIds = (List<String>) request.get("scopeIds");
            if (newScopeIds != null && newScopeIds.size() > 1) {
                return CommonResult.error("当前设计只允许一个租户绑定一个 scope_id");
            }
            
            // 获取旧的scopeIds
            List<String> oldScopeIds = new ArrayList<>();
            if (existingTenant.getScopeIds() != null && !existingTenant.getScopeIds().isEmpty()) {
                oldScopeIds = objectMapper.readValue(existingTenant.getScopeIds(), new TypeReference<List<String>>(){});
            }
            
            // 释放旧scope
            if (!oldScopeIds.isEmpty()) {
                scopeRegistryService.batchReleaseScopes(oldScopeIds);
            }
            
            // 分配新 scope
            if (newScopeIds != null && !newScopeIds.isEmpty()) {
                existingTenant.setScopeIds(objectMapper.writeValueAsString(newScopeIds));
                scopeRegistryService.batchAssignScopesToTenant(newScopeIds, tenantId);
            } else {
                // 清除租户的 scope 绑定，设置为空字符串而不是 null
                existingTenant.setScopeIds("[]");
                // 显式释放 Scope（更新到 unassigned 状态）
                for (String oldScopeId : oldScopeIds) {
                    scopeRegistryService.lambdaUpdate()
                        .eq(ScopeRegistry::getScopeId, oldScopeId)
                        .set(ScopeRegistry::getStatus, "unassigned")
                        .update();
                }
            }
                        
            boolean success = tenantService.updateById(existingTenant);
            if (!success) {
                log.error("【租户更新失败】Tenant ID: {}, Updated scopeIds: {}", tenantId, existingTenant.getScopeIds());
            }
            if (success) {
                return CommonResult.success(existingTenant);
            } else {
                return CommonResult.error("更新租户失败");
            }
        } catch (Exception e) {
            return CommonResult.error("更新租户失败：" + e.getMessage());
        }
    }
    
    /**
     * 删除租户
     * 删除前会：
     * 1. 查询该租户绑定的模板信息（用于前端提示）
     * 2. 查询该租户下的用户数量（用于前端提示）
     * 3. 删除该租户下的所有用户
     * 4. 清除 tenant_scope_config 表中的绑定记录
     * 5. 释放 Scope
     * 6. 删除租户
     */
    @DeleteMapping("/{tenantId}")
    @Transactional(rollbackFor = Exception.class)
    public CommonResult<Map<String, Object>> delete(@PathVariable String tenantId) {
        try {
            // 保护默认租户不被删除
            if ("tenant_001".equals(tenantId) || "tenant_default".equals(tenantId)) {
                return CommonResult.error("默认租户不允许删除");
            }
                
            Tenant tenant = tenantService.getById(tenantId);
            if (tenant == null) {
                return CommonResult.error("租户不存在");
            }
                
            // 查询该租户绑定的模板信息（用于前端提示）
            String scopeName = null;
            String templateName = null;
            int userCount = 0;
                
            // 查询该租户下的用户数量
            QueryWrapper<com.openjiuwen.memory.authcenter.domain.User> userWrapper = new QueryWrapper<>();
            userWrapper.eq("tenant_id", tenantId);
            userCount = (int) userService.count(userWrapper);
            
            // 删除该租户下的所有用户
            if (userCount > 0) {
                userService.remove(userWrapper);
            }
                
            // 获取 Scope 名称
            if (tenant.getScopeIds() != null && !tenant.getScopeIds().isEmpty()) {
                List<String> scopeIds = objectMapper.readValue(tenant.getScopeIds(), new TypeReference<List<String>>(){});
                if (!scopeIds.isEmpty()) {
                    // 按 scopeId 字段查询，而不是主键 id
                    QueryWrapper<ScopeRegistry> scopeWrapper = new QueryWrapper<>();
                    scopeWrapper.eq("scope_id", scopeIds.get(0));
                    ScopeRegistry scope = scopeRegistryService.getOne(scopeWrapper);
                    if (scope != null) {
                        scopeName = scope.getScopeName();
                    }
                }
            }
                
            // 查询该租户在 tenant_scope_config 中的记录，获取模板名称
            QueryWrapper<TenantScopeConfigEntity> configWrapper = new QueryWrapper<>();
            configWrapper.eq("tenant_id", tenantId);
            TenantScopeConfigEntity config = tenantScopeConfigMapper.selectOne(configWrapper);
            if (config != null && config.getTemplateId() != null) {
                ConfigTemplateEntity template = configTemplateMapper.selectById(config.getTemplateId());
                if (template != null) {
                    templateName = template.getTemplateName();
                }
                // 清除模板中的租户绑定记录
                tenantScopeConfigMapper.delete(configWrapper);
            }
                
            // 释放租户的所有 scope
            if (tenant.getScopeIds() != null && !tenant.getScopeIds().isEmpty()) {
                List<String> scopeIds = objectMapper.readValue(tenant.getScopeIds(), new TypeReference<List<String>>(){});
                scopeRegistryService.batchReleaseScopes(scopeIds);
            }
                
            // 删除租户
            boolean success = tenantService.removeById(tenantId);
            if (success) {
                // 返回删除结果，包含模板、Scope 和用户信息（用于前端提示）
                Map<String, Object> result = new HashMap<>();
                result.put("scopeName", scopeName);
                result.put("templateName", templateName);
                result.put("userCount", userCount);
                return CommonResult.success(result);
            } else {
                return CommonResult.error("删除租户失败");
            }
        } catch (Exception e) {
            return CommonResult.error("删除租户失败：" + e.getMessage());
        }
    }
}
