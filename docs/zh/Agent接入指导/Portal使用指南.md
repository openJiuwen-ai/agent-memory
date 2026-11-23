# Portal 使用指南

[English](<../../en/Agent Integration Guide/Portal User Guide.md>)

Portal 复用应用已接入的记忆实例。接入完成后，从应用的「记忆管理」入口打开 Portal，即可查看和管理当前用户有权访问的个人记忆。

## 打开 Portal

如果你在开发接入应用，按接入方式为当前用户获取访问链接。每次打开都获取新链接，并把完整地址（包括 `#` 后的片段）交给提供 Portal 的主机上的浏览器。链接包含一次性授权，避免将它写入日志或保存为书签。

### 通过 Provider 接入的 SDK 或 HTTP

继续使用已经按宿主的身份和记忆范围初始化的 `AgentMemoryMemoryProvider`。例如，桌面应用可以将下面的回调绑定到「记忆管理」菜单。

```python
import webbrowser


async def open_memory_portal(provider):
    """Open a fresh Portal link in the desktop browser."""
    url = await provider.open_portal()
    if url is None:
        return False
    return webbrowser.open(url)
```

Web 应用由当前用户的浏览器打开返回的链接。如果宿主在首次对话时才初始化 Provider，需要在提供入口前完成原有的初始化和绑定流程。宿主可用 `provider.portal_status["supported"]` 决定是否显示入口。

通过 Provider 的 `portal` 构造参数设置页面选项，例如 `portal={"language": "en"}`。启用 Portal 的 SDK Provider 会在初始化时尝试启动本机页面服务。HTTP 模式使用原记忆服务的端口。

| 选项 | 用法 |
| --- | --- |
| `enabled` | 控制 Provider 的 Portal 入口，默认 `True`。 |
| `port` | SDK 页面端口，默认 `0`，由系统分配空闲端口。也可以指定固定端口。 |
| `language` | SDK 和 HTTP 新链接的首屏语言。`en` 为英文，`zh` 或 `zh-CN` 为简体中文，缺省或留空时为简体中文。 |
| `instance_id` | 共享 Portal 的 SDK 文档实例标识，默认 `default`。为共用页面的各个独立实例配置不同的 ID，重启时沿用原 ID。 |

同一系统账号下，指向同一个固定本地 Markdown 目录并从同一个本地 SQLite 索引读取的 SDK 文档实例会共用 Portal。目录和索引都须使用绝对路径，Windows 上还须位于本地 NTFS。共用页面时，可在「模型设置」选择要配置的实例。

### 直接持有 SDK Runtime

如果应用通过 `assemble_runtime()` 直接接入，在启动时为已有的 `runtime` 挂载一次 Portal。将宿主的可信身份回调作为 `get_current_user_context` 传入。处理记忆或设置请求时，Portal 会通过回调重新获取身份，并核对它是否与挂载时绑定的身份一致。

```python
from jiuwen_memory_entry.portal.hosting import start_portal

portal = start_portal(runtime, security_provider=get_current_user_context)
```

每次用户打开页面时调用 `portal.launch_url()` 签发新链接。宿主退出时先调用 `portal.close()`，再关闭 Runtime。

### 直接调用 HTTP API

从宿主侧向已运行的 HTTP 服务发送 `POST /portal/api/launch`，沿用当前用户访问记忆服务的凭据。以下示例使用本机 `8137` 端口和 Bearer 凭据。`MEMORY_ACCESS_TOKEN` 为终端中已设置的记忆服务访问凭据。使用固定 `dev` 身份在本机联调时可省略 `Authorization` 请求头。

```bash
portal_base_url=http://127.0.0.1:8137
curl -fsS "${portal_base_url}/portal/api/launch" \
  -H "Origin: ${portal_base_url}" \
  -H "Authorization: Bearer ${MEMORY_ACCESS_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{}'
```

用本机浏览器打开返回的 `portal_url`。HTTP Portal 要求使用回环地址和服务实际监听的端口，`Origin` 须与请求所用的服务地址一致。若入口需绑定已有记忆轨的写入范围，通过 `write_target` 传入对应的 `space`、`agent` 和 `session`。字段说明见 [Portal 接口规约](../../specs/S02-memory-api.md#portal-页面操作与接口对应)。

### 嵌入宿主设置页

使用 iframe 时，将宿主页面的精确 origin（协议、主机及使用的端口）加入允许列表。SDK Provider 使用 `portal.allowed_frame_origins`，HTTP 服务使用启动配置中的 `http.portal_frame_origins`。宿主为当前用户取得新链接后，将完整链接设为 iframe 的 `src`。重新挂载 iframe 时重新发链。

## 查看和管理记忆

1. 「记忆概览」展示全部记录、有效记忆、纠正历史、已遗忘、已归档和来源应用数。
   「每日新增」按浏览器时区展示近 365 天所有状态的写入。「来源分布」按有效记忆计算占比，包含未标注来源。
   点击记录数量卡或来源分布中的应用可打开对应列表。
2. 在「我的记忆」默认查看有效记忆。「按文字查找」匹配正文或标签，可结合「筛选」里的来源应用、Agent、项目、分类等条件。想用问题检索时，切到「按含义查找」，输入问题，按需设置来源和返回数量，再点击「查找」。按含义查找只支持按来源应用筛选。左侧的「用户画像」「项目知识」「过程与做法」可直接打开相应分类视图。
3. 点击「新增记忆」，填写正文和可选项目，再点击「保存记忆」。如果宿主已绑定记忆轨，新增记录使用该范围，表单里的 Agent 会锁定。Agent 可编辑时，留空即保存为个人记忆。
4. 打开有效记忆的详情，点击「纠正」，编辑正文并预览后确认。旧版本保留在「纠正历史」。点击「遗忘」并确认后，记录退出有效列表和默认检索，仍可从「已遗忘」中核对。
5. 勾选记录或「选择本页」后，可以复制、导出 Markdown 或 JSON。所选记录均有效时，还可点击「遗忘所选」。批量遗忘逐条处理，完成后可在提示中查看失败记录。

点击顶栏的「English」或「中文」即可切换界面语言，当前筛选和检索结果会保留。

## 修改模型设置

1. 打开「模型设置」。如果页面提供「配置实例」下拉框，先选中需要修改的实例。
2. 修改页面开放的模型字段。更换模型服务时，同时填写新服务的完整网址、模型名和 API Key。继续使用同一服务时，API Key 留空会保留原凭据。
3. 点击「预览更改」，核对后点击「确认保存并应用」。显示「已应用」后，后续模型调用使用新设置。若显示「已保存，尚未应用」，可以点击「重试应用已保存配置」。
4. 要撤回某个已保存的覆盖值，在该字段点击「恢复启动值」，再预览并确认。成功保存的改动会在实例下次启动时加载。

模型设置能否编辑取决于宿主的授权和配置存储。页面会显示字段的只读状态或配置不可用的提示。Embedding 模型、向量维度和存储位置只读。

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 页面提示访问会话已失效 | 从应用重新打开，或重新调用 HTTP 发链接口。每次使用新的授权链接。 |
| SDK 无法打开 Portal | 查看 `portal_status["reason"]`。固定端口被占用时，释放端口后重新打开，或将 `portal.port` 设为 `0` 后重启。共享实例的 ID 冲突时，给冲突实例配置独立 ID 后重启。 |
| HTTP 发链返回 `404` 或 `401` | `404` 时检查回环地址、实际监听端口和 `Origin` 是否一致。`401` 时检查访问凭据是否有效。 |
| 列表里找不到已有记忆 | 进入「我的记忆」，清除文字和其他筛选，再把状态切到「全部」。确认记忆已写入当前实例，并且当前身份有权查看该记录。 |
| 能读记忆，模型设置却不可编辑 | 由宿主检查是否启用了配置管理、配置库位置是否可写，以及当前身份是否有配置权限。HTTP 自动启用配置管理需要声明固定个人身份，宿主也可以显式装配配置存储和授权。 |

更多接入选项见 [Portal 设计说明](../../features/F05-personal-memory-portal.md#6-启动与打开页面)。
