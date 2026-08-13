# whl 唯一顶层包名（src → jiuwen_memory）

## 元信息
| 项 | 值 |
|----|-----|
| 日期 | 2026-08-11 |
| 影响范围 | `jiuwen_memory/`（原 `src/`），`bootstrap/`，`agent_plugin/`，`pyproject.toml`，`deploy/docker/`（镜像 COPY 与 compose 挂载），测试 / evaluation / examples |
| 测试基线 | `pytest -q -m unit tests/unit` 通过；whl `top_level.txt` = `agent_plugin` / `bootstrap` / `jiuwen_memory`；scripts `PYTHONPATH=<repo>:<repo>/bootstrap/core`；`deploy/docker/{local,online,postgres}/Dockerfile` 可 `docker compose build agent-memory`（`COPY jiuwen_memory`，compose 挂载 `jiuwen_memory:/app/jiuwen_memory`） |
| Refs | #117 |

## 背景

mem2.0 以 `src/{api,common,config,...}` 平铺布局 + setuptools `include=api*` 打 whl，导致 `top_level.txt` 暴露 9 个通用顶层名。宿主项目若自带 `common/` / `config/` / `api/`，`sys.path` 优先 shadow，`JiuwenMemory` SDK 进程内路径在 `initialize` 即 `ModuleNotFoundError: No module named 'common.type_def'`。

内核目录改名后，`deploy/docker/*` 仍 `COPY src ./src` 且 compose 挂载 `../../../src:/app/src`，**镜像构建期即失败**（仓库已无 `src/`），与 whl/开发态不同步。

## 决策

1. **目录**：仓库内核目录 `src/` **改名为** `jiuwen_memory/`（非 `src/jiuwen_memory/` 嵌套）。
2. **import**：绝对引用统一为 `jiuwen_memory.*`；开发态 `pytest.pythonpath = ["."]` / `PYTHONPATH=.`，与安装态一致。
3. **whl 顶层**：`jiuwen_memory` + `agent_plugin` + `bootstrap`；**不**把后两者并入 `jiuwen_memory` 包内。
4. **bootstrap**：补 `bootstrap/__init__.py` 并 `include bootstrap*`，安装后可 `from bootstrap.xxx import ...`；surface 侧暂保留 flat `import server` / path-hack（`.sh` 仍可用），内核侧为 `jiuwen_memory.*`（不可再把 `jiuwen_memory/` 目录当 flat root）。
5. **deploy/docker**：与 rename 同步，凡引用内核源码的路径一律 `src` → `jiuwen_memory`：
   - **Dockerfile**（`local` / `online` / `postgres`）：`COPY jiuwen_memory ./jiuwen_memory`，`pip install -e .` 的 editable 目标为 `/app/jiuwen_memory`。
   - **docker-compose**（同上三套件）：开发热更新挂载 `../../../jiuwen_memory:/app/jiuwen_memory:ro`（替代原 `src:/app/src`）。
   - **bootstrap** 仍 `COPY` + 挂载 `bootstrap/`，HTTP 入口不变（`python bootstrap/http_server/__main__.py`）；**不在** Dockerfile 里为 flat `import server` 额外设 `PYTHONPATH`（`__main__.py` 已 append `bootstrap/core`）。
   - **文档注释**：`deploy/docker/local/config.yml`、`deploy/docker/README.md` 中 `src/config` 改为 `jiuwen_memory/config`。

## 拒绝的方案

| 方案 | 原因 |
|------|------|
| 只改 `packages.find`、不改 import | 安装后仍无顶层 `common`，包内 `from common` 必挂 |
| `PYTHONPATH=jiuwen_memory` 继续 flat-import | 开发假绿、与 site-packages 分叉，#117 会再现 |
| 把 `agent_plugin` / `bootstrap` 挂到 `jiuwen_memory.*` | 本轮明确要求二者保持仓库根独立顶层 |
| 只文档约定「宿主勿用 common」 | 通用名冲突无法避免，属反模式 |
| deploy 继续 `COPY src` 或软链 `src→jiuwen_memory` | 与仓库真实布局分叉，editable 挂载与 CI 易漏改；软链在 Windows/部分构建上下文不可靠 |

## 验证

- 构建 whl 后 `top_level.txt` 无 `api/common/config/...`，含 `bootstrap`、`agent_plugin`、`jiuwen_memory`
- 临时目录放置空 `common/` 并置顶 `PYTHONPATH`，仍可 `import jiuwen_memory.api` / `bootstrap.core`
- `pytest -m unit tests/unit`
- `deploy/docker` 下无残留 `src` 路径引用；`docker compose -f deploy/docker/online/docker-compose.yml build agent-memory` 能通过 `COPY jiuwen_memory` 阶段（完整栈需 Milvus/ES 等依赖服务，build 本身不依赖它们）

## 已知遗留

- bootstrap surface 仍有 flat path-hack。
- `agent_plugin` / `bootstrap` 仍是相对通用的顶层名，极端宿主冲突未消。
- 全库 docs/specs/features 中仍有大量历史 `src/` 路径与 `from api import` 示例，需按需渐进刷新（本轮已改根 `AGENTS.md`、内核 `AGENTS.md` 标题与 hook、`deploy/docker`）。
- 对外为 breaking change：调用方须改 import 前缀。
