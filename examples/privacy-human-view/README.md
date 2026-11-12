# 用户访问控制交互示例

本目录是独立静态演示，使用虚构记忆和模拟认证结果，不连接真实记忆服务，
不创建真实 Passkey，也不提供安全保障。页面内的示例原文可在源文件中读取。

正式业务以安全路由、MemoryAPI 用户访问控制入口和 `HumanView` 转发能力为接入契约；此页面用于说明用户交互，
不作为内核或 HTTP 服务的前端产品，不纳入 Python 安装包。

## 运行

在仓库根目录执行：

```bash
python -m http.server 9417 --bind 127.0.0.1 --directory examples/privacy-human-view
```

浏览器打开 <http://127.0.0.1:9417/>。仅依赖 Python 标准库和浏览器，无需安装前端工具。
也可以直接打开 `index.html`。若端口被占用，换一个空闲端口即可。

## 演示内容

1. 选择一条记忆，阅读脱敏正文和组织、空间、归属用户名。
2. 点击“验证并查看原文”，再点击“模拟验证通过”，在完整句子中展示原文。
3. 点击“清除原文”或切换记忆，恢复脱敏展示；取消验证不释放原文。
4. 在验证框点击“模拟验证期间记忆更新”，提示重新验证，原文保持隐藏。
5. 选择关闭还原的示例，只显示脱敏内容；公开内容无需还原。

页面不要求用户填写 Scope、记忆 ID 或 `privacy_revision`。这里的名称均为展示样例，
没有组织或用户目录查询；列表与验证状态均为本地模拟。

## 真实业务接线

此示例没有发起下面的请求。宿主接入时应沿用现有接口：

| 操作 | 接口与职责 |
|---|---|
| 获取脱敏记忆 | 使用已有授权链路调用 `/v1/list`、`/v1/search` 或 `/v1/get` |
| 初始化查看终端 | `GET /security/privacy` 建立服务端浏览器绑定 cookie |
| 开始登记或查看 | `POST /security/privacy/begin`；查看先常规认证、查询并暂存脱敏快照，再返回挑战及 target |
| 提交本人证明 | `POST /security/privacy/finish` 验证证明并复验权限和当前版本 |

宿主从已授权的真实记忆结果中自动取得完整 Scope、ID 和
`system_metadata.privacy_revision`，为本次查看传入 `scope`、`unit_id` 和可选的 `revision`。
访问 begin 必须经过宿主现有常规认证；finish 将 begin 返回的 target 中
scope/unit_id/revision 展开到请求顶层，与 challenge_id/credential 并列。
HTTP begin/finish 分别调用 MemoryAPI.begin_human_view/finish_human_view，认证、数据获取
和响应都经过 API；Runtime 的 HumanView 只作转发。快照与挑战绑定，认证期间发生修改、
删除或撤权时不还原；原文经 Engine 专用路径回到 API，再返回页面。
组织名和用户名仅用于展示，不能直接拼成身份或替代授权；查看者与记忆归属人可能不同。

真实前端通过浏览器 `navigator.credentials.create/get` 完成 Passkey 登记或验证，
不能用本示例的“模拟验证通过”代替。登记邀请由可信部署方绑定固定用户并独立交付。
`finish` 仍执行人的证明、资源权限、ACTIVE 状态和版本检查；宿主不缓存、转发给模型
或通过普通检索接口释放还原后的原文，不向浏览器注入机器服务密钥。

真实 HTTP 查看须与配置的 origin 同源；本静态示例使用独立端口不代表已建立该信任关系。
完整配置和协议见 [配置示例](../privacy-human-view.yml) 和
[S10 安全规约](../../docs/specs/S10-security.md#用户访问控制-http-接口)。
