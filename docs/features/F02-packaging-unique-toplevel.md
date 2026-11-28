# whl 唯一顶层包名（src → jiuwen_memory）

## 元信息
| 项 | 值 |
|----|-----|
| 日期 | 2026-08-11 |
| 影响范围 | `jiuwen_memory/`（原 `src/`），`jiuwen_memory_entry/`，`jiuwen_memory_adapter/`，`pyproject.toml`，`deploy/docker/`（镜像 COPY 与 compose 挂载），测试 / evaluation / examples |
| 测试基线 | `pytest -q -m unit tests/unit` 通过；whl `top_level.txt` = `jiuwen_memory_adapter` / `jiuwen_memory_entry` / `jiuwen_memory`；scripts `PYTHONPATH=<repo>:<repo>/jiuwen_memory_entry/core`；`deploy/docker/{local,online,postgres}/Dockerfile` 可 `docker compose build agent-memory`（`COPY jiuwen_memory`，compose 挂载 `jiuwen_memory:/app/jiuwen_memory`）；whl `entry_points.txt` 含三个 console_scripts，干净 venv 安装后 `agent-memory` / `agent-memory-server` / `agent-memory-mcp` 可直接调用 |
| Refs | #117；#317（2026-11-12 入口点回归修复，见「决策 6」） |

## 背景

mem2.0 以 `src/{api,common,config,...}` 平铺布局 + setuptools `include=api*` 打 whl，导致 `top_level.txt` 暴露 9 个通用顶层名。宿主项目若自带 `common/` / `config/` / `api/`，`sys.path` 优先 shadow，`JiuwenMemory` SDK 进程内路径在 `initialize` 即 `ModuleNotFoundError: No module named 'common.type_def'`。

内核目录改名后，`deploy/docker/*` 仍 `COPY src ./src` 且 compose 挂载 `../../../src:/app/src`，**镜像构建期即失败**（仓库已无 `src/`），与 whl/开发态不同步。

## 决策

1. **目录**：仓库内核目录 `src/` **改名为** `jiuwen_memory/`（非 `src/jiuwen_memory/` 嵌套）。
2. **import**：绝对引用统一为 `jiuwen_memory.*`；开发态 `pytest.pythonpath = ["."]` / `PYTHONPATH=.`，与安装态一致。
3. **whl 顶层**：`jiuwen_memory` + `jiuwen_memory_adapter` + `jiuwen_memory_entry`；**不**把后两者并入 `jiuwen_memory` 包内。
4. **bootstrap**：补 `jiuwen_memory_entry/__init__.py` 并 `include jiuwen_memory_entry*`，安装后可 `from jiuwen_memory_entry.xxx import ...`；surface 侧暂保留 flat `import server` / path-hack（`.sh` 仍可用），内核侧为 `jiuwen_memory.*`（不可再把 `jiuwen_memory/` 目录当 flat root）。
5. **deploy/docker**：与 rename 同步，凡引用内核源码的路径一律 `src` → `jiuwen_memory`：
   - **Dockerfile**（`local` / `online` / `postgres`）：`COPY jiuwen_memory ./jiuwen_memory`，`pip install -e .` 的 editable 目标为 `/app/jiuwen_memory`。
   - **docker-compose**（同上三套件）：开发热更新挂载 `../../../jiuwen_memory:/app/jiuwen_memory:ro`（替代原 `src:/app/src`）。
   - **bootstrap** 仍 `COPY` + 挂载 `jiuwen_memory_entry/`，HTTP 入口不变（`python jiuwen_memory_entry/http_server/__main__.py`）；**不在** Dockerfile 里为 flat `import server` 额外设 `PYTHONPATH`（`__main__.py` 已 append `jiuwen_memory_entry/core`）。
   - **文档注释**：`deploy/docker/local/config.yml`、`deploy/docker/README.md` 中 `src/config` 改为 `jiuwen_memory/config`。
6. **命令面入口点（2026-11-12 追加）**：`[project.scripts]` 声明三个命令；CLI / HTTP 直接指向各 surface 的 `__main__:main`，MCP 指向薄包装 `_console:main`（原因见下）：
   - `agent-memory` → `jiuwen_memory_entry.cli.__main__:main`
   - `agent-memory-server` → `jiuwen_memory_entry.http_server.__main__:main`
   - `agent-memory-mcp` → `jiuwen_memory_entry.mcp_server._console:main`

   同时**移除** `packages.find.exclude` 对 `jiuwen_memory_entry.cli*` / `jiuwen_memory_entry.mcp_server*` 的排除——这两条 exclude 由 !285 加入且未记录理由，与「安装后即可用命令面」直接冲突，曾导致 `pip install` 后只有 SDK、38+ MCP 工具与 CLI 全部不可见。命令名与各 `__main__.py` 的 argparse `prog` 保持一致（`agent-memory` / `agent-memory-server`），避免 `--help` 显示的程序名与实际命令不符。

   命令面入口点不是新能力：`a3e3fcd`（2026-06-26）曾以 `memory-server = "server.memory_server:main"` 交付过，!280 改名与 !285 exclude 之间丢失了这条链路。本次按当前 `jiuwen_memory_entry` 布局恢复并扩到三个 surface。

   `agent-memory-mcp` 指向 `mcp_server/_console.py` 而非 `__main__`：后者在导入期就要求 mcp SDK 并抛 `RuntimeError`（内核侧失闭语义，库调用方需要）。命令调用方要的是可执行提示而不是 traceback，故薄包装把导入期的 `RuntimeError` / `ValidationError` 转成一行 stderr 提示 + 退出码 2，不降级为静默成功。包装只按消息前缀过滤内核侧重复的 `required import failed` 日志，**不改 logger 级别**——`development authentication is enabled` 等安全告警必须照常输出（曾用 `setLevel(ERROR)` 压制，会连安全告警一起吞掉，已修正并加测试锁住）。

## 拒绝的方案

| 方案 | 原因 |
|------|------|
| 只改 `packages.find`、不改 import | 安装后仍无顶层 `common`，包内 `from common` 必挂 |
| `PYTHONPATH=jiuwen_memory` 继续 flat-import | 开发假绿、与 site-packages 分叉，#117 会再现 |
| 把 `jiuwen_memory_adapter` / `jiuwen_memory_entry` 挂到 `jiuwen_memory.*` | 本轮明确要求二者保持仓库根独立顶层 |
| 只文档约定「宿主勿用 common」 | 通用名冲突无法避免，属反模式 |
| deploy 继续 `COPY src` 或软链 `src→jiuwen_memory` | 与仓库真实布局分叉，editable 挂载与 CI 易漏改；软链在 Windows/部分构建上下文不可靠 |
| 入口点指向 `scripts/run-*.sh` | 脚本不在 wheel 内、依赖 `PYTHONPATH` 与 `python3` 环境，安装态无法定位；console_scripts 直接指向模块 `main`，不依赖 shell |
| 只加 `[project.scripts]` 而保留 exclude | target 模块不在 wheel 内，安装后 console_script 立即 `ModuleNotFoundError`——两处必须同改 |
| 维持 !285 的 exclude（cli / mcp_server 不进 wheel） | !285（2026-09-04）只加 4 行 exclude、无理由说明，关联 issue #186 是泛化的「release 文档补充、架构整改适配」，评审仅 `/lgtm`。它使 `pip install` 后命令面整体不可见，与 !280 改名时把 `jiuwen_memory_entry` 纳入 include 的方向相反。历史证据：`a3e3fcd`（2026-06-26「支持 memory_server 一行命令启动」）曾用 `[project.scripts]` 交付过命令面，!285 是回归而非新约束 |

## 验证

- 构建 whl 后 `top_level.txt` 无 `api/common/config/...`，含 `jiuwen_memory_entry`、`jiuwen_memory_adapter`、`jiuwen_memory`
- 临时目录放置空 `common/` 并置顶 `PYTHONPATH`，仍可 `import jiuwen_memory.api` / `jiuwen_memory_entry.core`
- `pytest -m unit tests/unit`
- `deploy/docker` 下无残留 `src` 路径引用；`docker compose -f deploy/docker/online/docker-compose.yml build agent-memory` 能通过 `COPY jiuwen_memory` 阶段（完整栈需 Milvus/ES 等依赖服务，build 本身不依赖它们）
- **入口点（2026-11-12）**：`uv build --wheel` 后 `unzip -p <whl> '*/entry_points.txt'` 含三个 console_scripts；`unzip -l <whl> | grep -c 'jiuwen_memory_entry/\(cli\|mcp_server\)/'` 为 7（非 0）；干净 venv `pip install <whl>` 后 `agent-memory --help` / `agent-memory-server --help` 退出码 0，`agent-memory add --auth-mode dev ...` 端到端返回记忆 JSON；`agent-memory-mcp` 在未装 extra 时报 `pip install ".[mcp]"`，装上后 `_build_server()` 装配成功、注册 41 个工具。回归锁：`tests/unit/jiuwen_memory_entry/test_entry_points.py` 6 项。关键设计——exclude 匹配用 `fnmatch.fnmatchcase` 而非子串（setuptools `packages.find` 正是用 fnmatch；子串会放过 `jiuwen_memory_entry.*`、`*server*` 这类同样能排除该包的写法）；函数定义与 FastMCP 显示名用 AST 判定而非源码字符串（注释掉的 `def main(` 与排版变化都不再误判）；命令名直接比对 `build_parser().prog`，不跑 `--help`（会抛 `SystemExit`，与华为编码规范 G.ERR.11 冲突）；`_console` 的行为（缺 SDK 时退出码 2、不吐 traceback、不改 logger 级别）有独立测试。反向验证覆盖四种 exclude 写法、删除 `[project.scripts]`、改 HTTP `prog`、恢复 `setLevel` 压制。质量门：CRAP 最高 3.0（阈值 10，覆盖率 100%）；`dryer --threshold 0.8` 对本轮改动无候选；变异测试不适用——生产代码改动仅 parser 抽取与 `_console` 转发。

## 已知遗留

- bootstrap surface 仍有 flat path-hack。
- `jiuwen_memory_adapter` / `jiuwen_memory_entry` 仍是相对通用的顶层名，极端宿主冲突未消。
- 本文前文保留 `src/` 与旧顶层 import 作为迁移历史证据；当前文档中的可执行 import 示例已统一使用 `jiuwen_memory.*`，后续新增示例也必须遵循该前缀。
- 对外为 breaking change：调用方须改 import 前缀。
- **库式导入仍失败（2026-11-12 实测）**：`import jiuwen_memory_entry.core.server` → `ModuleNotFoundError: No module named 'profiles'`，`import jiuwen_memory_entry.mcp_server.transport_security` → `No module named 'auth_middleware'`。根因是 `core/server.py:32`、`mcp_server/transport_security.py:16` 的 flat import；命令面不受影响（各 `__main__.py` 先注入 `core` 路径），但「装上能跑命令」≠「包可被当库导入」。修复需把 surface 侧 flat import 统一为 `jiuwen_memory_entry.core.*` 并移除 `sys.path` 注入，触及装配路径，单独立项。
- **`agent-memory-mcp --help` 崩溃**：模块级 `_build_server()` 把 `sys.argv[1:]` 当配置路径，`--help` 被当作文件名 → `FileNotFoundError`。暴露为命令后更易触发；修复需把 MCP 模块级装配改为惰性（F03 范围）。
- 命令面入口点已随 wheel 交付，但 `scripts/run-*.sh` 仍保留——开发态（未安装、直接跑源码树）依赖它们设置 `PYTHONPATH`，两套入口并存是有意为之。
