#!/usr/bin/env node

/**
 * 前端 API 接口测试脚本
 * 用于验证前端是否能正确调用后端租户与用户管理接口
 */

import axios from 'axios';

const BASE_URL = 'http://localhost:9000/api/v1';
let token = '';

// 颜色输出
const colors = {
  reset: '\x1b[0m',
  green: '\x1b[32m',
  red: '\x1b[31m',
  yellow: '\x1b[33m',
  cyan: '\x1b[36m',
};

function log(color, message) {
  console.log(`${color}${message}${colors.reset}`);
}

async function testAPI() {
  log(colors.cyan, '\n========================================');
  log(colors.cyan, '前端 API 接口测试');
  log(colors.cyan, '========================================\n');

  // 测试 1: 登录
  try {
    log(colors.yellow, '测试 1: 用户登录...');
    const loginRes = await axios.post(`${BASE_URL}/auth/login`, {
      username: 'admin',
      password: 'admin123',
    });
    
    if (loginRes.data.code === 0) {
      token = loginRes.data.data.token;
      log(colors.green, '✅ 登录成功！Token: ' + token.substring(0, 50) + '...\n');
    } else {
      log(colors.red, '❌ 登录失败: ' + loginRes.data.message + '\n');
      return;
    }
  } catch (error) {
    log(colors.red, '❌ 登录请求失败: ' + error.message + '\n');
    return;
  }

  const headers = { Authorization: `Bearer ${token}` };

  // 测试 2: 获取租户列表
  try {
    log(colors.yellow, '测试 2: 获取租户列表...');
    const res = await axios.get(`${BASE_URL}/tenants`, { headers });
    if (res.data.code === 0) {
      log(colors.green, `✅ 获取成功！共 ${res.data.data.length} 个租户\n`);
    }
  } catch (error) {
    log(colors.red, '❌ 失败: ' + error.message + '\n');
  }

  // 测试 3: 获取用户列表
  try {
    log(colors.yellow, '测试 3: 获取用户列表...');
    const res = await axios.get(`${BASE_URL}/users`, { headers });
    if (res.data.code === 0) {
      log(colors.green, `✅ 获取成功！共 ${res.data.data.length} 个用户\n`);
    }
  } catch (error) {
    log(colors.red, '❌ 失败: ' + error.message + '\n');
  }

  // 测试 4: 获取角色列表
  try {
    log(colors.yellow, '测试 4: 获取角色列表...');
    const res = await axios.get(`${BASE_URL}/roles`, { headers });
    if (res.data.code === 0) {
      log(colors.green, `✅ 获取成功！角色: ${res.data.data.join(', ')}\n`);
    }
  } catch (error) {
    log(colors.red, '❌ 失败: ' + error.message + '\n');
  }

  // 测试 5: 创建测试租户
  let testTenantId = '';
  try {
    log(colors.yellow, '测试 5: 创建测试租户...');
    const res = await axios.post(
      `${BASE_URL}/tenants`,
      { name: '前端API测试租户', remark: '自动化测试创建' },
      { headers }
    );
    if (res.data.code === 0) {
      testTenantId = res.data.data.id;
      log(colors.green, `✅ 创建成功！租户ID: ${testTenantId}\n`);
    }
  } catch (error) {
    log(colors.red, '❌ 失败: ' + error.message + '\n');
  }

  // 测试 6: 创建测试用户
  let testUserId = '';
  try {
    log(colors.yellow, '测试 6: 创建测试用户...');
    const res = await axios.post(
      `${BASE_URL}/users`,
      {
        username: 'frontend_test_user',
        password: 'test123',
        role: 'READ_ONLY',
        tenant_id: 'tenant_default',
        remark: '前端API测试用户',
      },
      { headers }
    );
    if (res.data.code === 0) {
      testUserId = res.data.data.id;
      log(colors.green, `✅ 创建成功！用户ID: ${testUserId}\n`);
    }
  } catch (error) {
    log(colors.red, '❌ 失败: ' + error.message + '\n');
  }

  // 测试 7: 更新租户
  if (testTenantId) {
    try {
      log(colors.yellow, '测试 7: 更新测试租户...');
      const res = await axios.put(
        `${BASE_URL}/tenants/${testTenantId}`,
        { name: '前端API测试租户-已更新', remark: '更新测试' },
        { headers }
      );
      if (res.data.code === 0) {
        log(colors.green, '✅ 更新成功！\n');
      }
    } catch (error) {
      log(colors.red, '❌ 失败: ' + error.message + '\n');
    }
  }

  // 测试 8: 删除测试用户
  if (testUserId) {
    try {
      log(colors.yellow, '测试 8: 删除测试用户...');
      const res = await axios.delete(`${BASE_URL}/users/${testUserId}`, { headers });
      if (res.data.code === 0) {
        log(colors.green, '✅ 删除成功！\n');
      }
    } catch (error) {
      log(colors.red, '❌ 失败: ' + error.message + '\n');
    }
  }

  // 测试 9: 删除测试租户
  if (testTenantId) {
    try {
      log(colors.yellow, '测试 9: 删除测试租户...');
      const res = await axios.delete(`${BASE_URL}/tenants/${testTenantId}`, { headers });
      if (res.data.code === 0) {
        log(colors.green, '✅ 删除成功！\n');
      }
    } catch (error) {
      log(colors.red, '❌ 失败: ' + error.message + '\n');
    }
  }

  log(colors.cyan, '========================================');
  log(colors.green, '✅ 所有测试完成！');
  log(colors.cyan, '========================================\n');
}

// 运行测试
testAPI().catch(console.error);
