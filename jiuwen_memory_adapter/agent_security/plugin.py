"""第一阶段装配入口：共享来源、审批、审计和按 framework 选择的受控 agent。"""

import asyncio
import re
from contextvars import ContextVar
from dataclasses import replace
from types import MappingProxyType
from uuid import uuid4

from ._validation import has_exact_type
from .approval import ApprovalInbox
from .audit import SecurityAuditLog
from .backends import BackendContext, create_backend_registry
from .config import AgentSecurityConfig
from .coverage import CoverageReport
from .delivery import DeliveryContext, create_delivery_adapter
from .execution import create_execution_adapter
from .frameworks import RuntimeContext, load_runtime_driver
from .hooks import Hook
from .llm import LlmCallContext, create_llm_adapter
from .policy import EgressPolicy
from .results import ResultGuard
from .runtime import (
    Actor,
    ApprovalBroker,
    EgressGuard,
    Operation,
    PayloadInspector,
    SecurityDenied,
)
from .sources import SourceLedger


class SecurityPlugin:
    """可信宿主提供认证和授权端口；这些端口不从模型、请求体或聊天文字读取身份。"""

    def __init__(
        self,
        config,
        *,
        inspector=None,
        policy,
        targets,
        authorize,
        authorize_input,
        authorize_transfer,
        notify=None,
        audit_sink=None,
        backend_registry=None,
        runtime_registry=None,
    ):
        if inspector is None:
            from .inspection import create_text_inspector

            inspector = create_text_inspector()
        self.config = (
            config
            if isinstance(config, AgentSecurityConfig)
            else AgentSecurityConfig.from_mapping(config)
        )
        self.driver = load_runtime_driver(self.config, registry=runtime_registry)
        self.policy = EgressPolicy.from_mapping(policy) if isinstance(policy, dict) else policy
        self.inspector, self.authorize, self.authorize_transfer = (
            inspector,
            authorize,
            authorize_transfer,
        )
        self.sources, self.audit = SourceLedger(), SecurityAuditLog(sink=audit_sink)
        self.broker = ApprovalBroker(audit=self.audit)
        self.inbox = ApprovalInbox(self.broker, notify=self._notify if notify else None)
        self._notification, self._active = (
            notify,
            ContextVar(f"jiuwen_managed_{id(self)}", default=None),
        )
        self.results = ResultGuard(
            self.sources, inspector, authorize_input=authorize_input, audit=self.audit
        )
        self._parents, self._models, self._tools = {}, {}, {}
        self._clients, self._closed = [], False
        self._built = False
        self.backend_registry = backend_registry or create_backend_registry()
        self._memory_backends, self._backend_names = [], set()
        self._named_tools, self._backend_tools = {}, {}
        self.targets = tuple(targets)
        if len({t.target_id for t in self.targets}) != len(self.targets):
            raise SecurityDenied("受控模型目标 ID 必须唯一")
        self._tool_targets = {}
        self.llm = create_llm_adapter(
            self.config,
            self._guard({"messages"}),
            targets=self.targets,
            sources=self.sources,
            results=self.results,
            audit=self.audit,
            driver=self.driver,
            context_for=self._llm_context,
            present=lambda public: self.inbox.present(public, actor=self.current_run().actor),
        )
        self.execution = create_execution_adapter(
            self.config,
            self._guard({"body"}),
            operation_for=self._tool_operation,
            sources=self.sources,
            results=self.results,
            audit=self.audit,
            driver=self.driver,
        )
        self.delivery = create_delivery_adapter(
            self.config,
            self._guard({"body"}),
            sources=self.sources,
            context_for=self._delivery_context,
            present=self.inbox.present,
            authorize_transfer=authorize_transfer,
            audit=self.audit,
            driver=self.driver,
        )
        self._runtime_context = RuntimeContext(
            self.llm,
            self.execution,
            self.results,
            self.current_run,
            MappingProxyType(self._models),
            MappingProxyType(self._tools),
            self._own_client,
        )

    def _guard(self, fields):
        return EgressGuard(
            PayloadInspector(self.inspector, frozenset(fields)),
            self.policy,
            self.broker,
            authorize=lambda operation: self.authorize(operation) is True,
            audit=self.audit,
        )

    @property
    def runtime_context(self):
        """只读收据与受控装配端口；不交付可写注册表。"""
        return self._runtime_context

    @property
    def closed(self):
        return self._closed

    def enter_run(self, run):
        self._ensure_open()
        return self._active.set(run)

    def leave_run(self, token):
        self._active.reset(token)

    def mark_bound(self):
        self._ensure_open()
        self._built = True

    def begin_delegate(self, actor, parent):
        owner = self.owner_for(parent)
        self.sources.begin(actor, fresh=True)
        self._parents[actor] = owner

    def current_run(self):
        value = self._active.get()
        if self._closed or value is None:
            raise SecurityDenied("受控 agent 运行上下文不存在")
        self.sources.snapshot(value.actor)
        return value

    def _ensure_open(self):
        if self._closed:
            raise SecurityDenied("安全装配已关闭")

    def _check_tool_name(self, name, *, backend=False):
        self._ensure_open()
        if (
            not has_exact_type(name, str)
            or re.fullmatch("[a-zA-Z0-9_-]{1,96}", name) is None
            or name in self._named_tools
        ):
            raise SecurityDenied("工具名称无效或与当前装配冲突")
        elif name in self._backend_names and (not backend):
            raise SecurityDenied("工具名称无效或与当前装配冲突")

    def _register_tool(self, name, tool):
        self._named_tools[name] = tool
        self._tools[id(tool)] = tool
        return tool

    def _llm_context(self, request):
        run = self.current_run()
        task = self.driver.llm_task(request)
        return LlmCallContext(run.actor, f"{run.turn}:{task}")

    def _tool_operation(self, context, payload):
        run = self.current_run()
        return Operation.create(
            actor=run.actor,
            operation_id=context.call_id,
            hook=Hook.BEFORE_TOOL_CALL,
            action=context.name,
            target=self._tool_targets[context.name],
            payload=payload,
            source_level="S1",
            source_version="overridden-by-ledger",
        )

    def _delivery_context(self, host, hook):
        run = self.current_run()
        if host is not run:
            raise SecurityDenied("交付不是当前受控调用")
        target = f"agent:{run.recipient.agent_id}" if run.recipient else "current-user"
        return DeliveryContext(
            run.actor,
            run.operation_id or f"{run.turn}:final",
            target,
            "agent.message" if run.recipient else "agent.response",
            run.recipient,
        )

    def owner_for(self, actor):
        if actor not in self._parents:
            raise SecurityDenied("未知会话不能访问安全控制通道")
        return self._parents[actor]

    def _notify(self, actor, event):
        self._notification(self.owner_for(actor), event)

    def new_session(self, authenticated_actor):
        if not isinstance(authenticated_actor, Actor) or self._closed:
            raise SecurityDenied("缺少宿主认证主体")
        actor = replace(
            authenticated_actor, session_id="jiuwen-" + uuid4().hex, incarnation=uuid4().hex
        )
        self.sources.begin(actor, fresh=True)
        self._parents[actor] = actor
        return actor

    def pending(self, actor):
        if self.owner_for(actor) != actor:
            raise SecurityDenied("审批须进入原始用户会话")
        requests = []
        for member, owner in tuple(self._parents.items()):
            if owner == actor:
                requests.extend(self.inbox.pending(member))
        return tuple(requests)

    def decide(self, *, actor, approval_id, version, choice):
        if self.owner_for(actor) != actor:
            raise SecurityDenied("审批会话所有者不匹配")
        for member, owner in tuple(self._parents.items()):
            if owner == actor and any(
                r["approval_id"] == approval_id for r in self.inbox.pending(member)
            ):
                self.inbox.decide(
                    actor=member, approval_id=approval_id, version=version, choice=choice
                )
                return
        raise SecurityDenied("本会话没有该审批请求")

    def connect_memory_backend(self, backend_id=None, **native_options):
        if self._closed:
            raise SecurityDenied("安全装配已关闭")
        selected = backend_id or self.config.memory_backend
        if not selected or (self.config.memory_backend and selected != self.config.memory_backend):
            raise SecurityDenied("记忆后端未选定或与配置不一致")
        backend = self.backend_registry.create(
            selected, BackendContext(self.sources, self.inspector, self.audit), **native_options
        )
        names = {operation.name for operation in backend.operations}
        if names & (self._backend_names | self._named_tools.keys()):
            backend.close()
            raise SecurityDenied("记忆后端接口名称冲突")
        self._backend_names.update(names)
        self._memory_backends.append(backend)
        return backend

    def memory_tools(self, backend):
        from .adapters._memory_tools import create_memory_tools

        if not any(item is backend for item in self._memory_backends) or self._closed:
            raise SecurityDenied("记忆后端不属于当前装配")
        key = id(backend)
        if key not in self._backend_tools:
            for operation in backend.operations:
                self._check_tool_name(operation.name, backend=True)
            tools = create_memory_tools(self, backend)
            for operation, tool in zip(backend.operations, tools, strict=True):
                self._register_tool(operation.name, tool)
            self._backend_tools[key] = tools
        return self._backend_tools[key]

    def tool(self, *, target, **definition):
        name = definition["name"]
        self._check_tool_name(name)
        if target not in self.policy.allowed_targets:
            raise SecurityDenied("工具目标未登记")
        tool = self.execution.create_tool(**definition)
        self._tool_targets[name] = target
        return self._register_tool(name, tool)

    def model(self, target_id, *, api_key, streaming=False, max_retries=1, **options):
        self._ensure_open()
        allowed = {"temperature", "top_p", "max_tokens", "timeout"}
        if set(options) - allowed or not has_exact_type(api_key, str) or not api_key:
            raise SecurityDenied("模型凭据或参数未覆盖")
        target = next((t for t in self.targets if t.target_id == target_id), None)
        if target is None or not target.endpoint.endswith("/chat/completions"):
            raise SecurityDenied("模型目标未登记")
        model = self.driver.create_model(
            self._runtime_context,
            target,
            api_key=api_key,
            streaming=streaming,
            max_retries=max_retries,
            **options,
        )
        self._models[id(model)] = (model, target)
        return model

    def _own_client(self, client, asynchronous):
        self._ensure_open()
        self._clients.append((client, asynchronous))

    async def agent(self, **configuration):
        from .managed import build_agent

        return await build_agent(self, **configuration)

    def delegate_tool(self, child, *, name):
        from .managed import ManagedAgent, delegate

        self._check_tool_name(name)
        if not isinstance(child, ManagedAgent) or not child.belongs_to(self):
            raise SecurityDenied("接收 agent 不属于当前安全装配")

        async def arun(body: str):
            return await delegate(self, child, body)

        tool = self.driver.create_tool(
            name=name,
            description=f"向 {child.agent_id} 发送受保护消息并等待受控回复。",
            schema={
                "type": "object",
                "properties": {"body": {"type": "string"}},
                "required": ["body"],
                "additionalProperties": False,
            },
            ainvoke=arun,
        )
        return self._register_tool(name, tool)

    def coverage(self):
        protected = {
            Hook.BEFORE_LLM_CALL,
            Hook.AFTER_LLM_CALL,
            Hook.ON_LLM_STREAM_CHUNK,
            Hook.BEFORE_AGENT_RESPONSE,
            Hook.BEFORE_AGENT_MESSAGE,
        }
        if self._memory_backends:
            protected.add(Hook.BEFORE_MEMORY_EXPORT)
        if self._tool_targets:
            protected.update({Hook.BEFORE_TOOL_CALL, Hook.AFTER_TOOL_CALL})
        if not self._built or self._closed:
            protected.clear()
        return CoverageReport(
            self.config.framework,
            frozenset(protected),
            (
                ("runtime-closed",)
                if self._closed
                else ("runtime-not-bound",)
                if not self._built
                else ()
            )
            + ("unregistered-tools", "unregistered-providers", "media", "unknown-history"),
            tuple(sorted(self._tool_targets)),
            tuple(sorted(t.target_id for t in self.targets)),
            memory_backends=tuple(sorted({b.backend_id for b in self._memory_backends})),
            memory_operations=tuple(sorted(self._backend_names)),
        )

    async def aclose(self):
        if self._closed:
            return
        # 先撤销运行权限，再逐个尝试释放；组件异常不能跳过剩余资源。
        self._closed = True
        errors = []

        def attempt(close):
            try:
                close()
            except BaseException as exc:
                errors.append(exc)

        for component in (self.inbox, self.delivery, self.execution, self.llm):
            attempt(component.close)
        clients, self._clients = self._clients, []
        for client, asynchronous in clients:
            if asynchronous:
                try:
                    await client.aclose()
                except BaseException as exc:
                    errors.append(exc)
            else:
                attempt(client.close)
        for component in (
            *self._memory_backends,
            self.results,
            self.sources,
            self.broker,
            self.audit,
        ):
            attempt(component.close)
        self._memory_backends.clear()
        self._backend_tools.clear()
        self._backend_names.clear()
        self._named_tools.clear()
        self._tools.clear()
        self._models.clear()
        self._parents.clear()
        # 保留任务取消语义，同时完成其他组件的清理。
        cancelled = next((e for e in errors if isinstance(e, asyncio.CancelledError)), None)
        if cancelled is not None:
            if len(errors) > 1:
                cancelled.add_note("安全装配关闭时另有组件清理失败")
            raise cancelled
        if errors:
            raise BaseExceptionGroup("安全装配部分资源清理失败", errors)

    def close(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aclose())
        raise RuntimeError("异步应用请 await plugin.aclose()")
