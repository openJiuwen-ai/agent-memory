"""文本 Chat 响应释放门禁；SSE 完整缓冲后检查，未经检查的字节不进入 SDK。"""

import json
import zlib

from ._validation import has_exact_type
from .hooks import Hook
from .runtime import SecurityDenied, encode_payload

LIMIT = 262144


def parse_json(value):
    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                raise SecurityDenied("模型结果包含重复键")
            result[key] = child
        return result

    try:
        result = json.loads(value, object_pairs_hook=pairs)
        encode_payload(result)
        return result
    except Exception:
        raise SecurityDenied("模型结果不是受支持的 JSON") from None


class ResponseBuffer:
    """限制解压后大小；不使用固定尾窗假设正则可安全跨块匹配。"""

    def __init__(self, encoding):
        if encoding not in {"", "identity", "gzip", "deflate"}:
            raise SecurityDenied("模型响应编码未覆盖")
        self.decoder = (
            zlib.decompressobj(31 if encoding == "gzip" else 15)
            if encoding in {"gzip", "deflate"}
            else None
        )
        self.parts, self.size, self.wire_size = [], 0, 0

    def add(self, data):
        self.wire_size += len(data)
        if self.wire_size > LIMIT:
            raise SecurityDenied("模型响应超出缓冲预算")
        if self.decoder is not None:
            data = self.decoder.decompress(data, LIMIT + 1 - self.size)
        self.size += len(data)
        if self.size > LIMIT or (self.decoder and self.decoder.unconsumed_tail):
            raise SecurityDenied("模型响应超出解压预算")
        self.parts.append(data)

    def finish(self):
        if self.decoder is not None and (not self.decoder.eof or self.decoder.unused_data):
            raise SecurityDenied("模型压缩响应不完整或含额外载荷")
        try:
            return b"".join(self.parts).decode("utf-8")
        except UnicodeError:
            raise SecurityDenied("模型响应不是 UTF-8 文本") from None


def completion_from_sse(text):
    chunks, done = [], False
    for event in text.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:")]
        if not data:
            if event.strip() and not all(line.startswith(":") for line in event.splitlines()):
                raise SecurityDenied("未知模型流事件")
            continue
        payload = "\n".join(data)
        if done:
            raise SecurityDenied("模型流结束后仍有数据")
        if payload == "[DONE]":
            done = True
        else:
            chunks.append(parse_json(payload))
        if len(chunks) > 4096:
            raise SecurityDenied("模型流事件数量超限")
    if not done or not chunks:
        raise SecurityDenied("模型流未完整结束")
    first, choices, usage = chunks[0], {}, None
    for chunk in chunks:
        if set(chunk) - {
            "id",
            "object",
            "created",
            "model",
            "choices",
            "usage",
            "system_fingerprint",
            "service_tier",
        }:
            raise SecurityDenied("模型流包含未覆盖字段")
        if any(chunk.get(k) != first.get(k) for k in ("id", "model")):
            raise SecurityDenied("模型流身份改变")
        if chunk.get("usage") is not None:
            usage = chunk["usage"]
        for part in chunk["choices"]:
            index = part["index"]
            if not has_exact_type(index, int) or not 0 <= index < 16:
                raise SecurityDenied("模型候选编号无效")
            if set(part) - {"index", "delta", "finish_reason", "logprobs"} or part.get("logprobs"):
                raise SecurityDenied("模型流候选字段未覆盖")
            item = choices.setdefault(
                index,
                {
                    "index": index,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": None,
                },
            )
            if item["finish_reason"] is not None:
                raise SecurityDenied("模型候选结束后仍有数据")
            delta, message = part.get("delta", {}), item["message"]
            if set(delta) - {"role", "content", "refusal", "reasoning_content", "tool_calls"}:
                raise SecurityDenied("模型增量类型未覆盖")
            if delta.get("role", "assistant") != "assistant":
                raise SecurityDenied("模型响应角色无效")
            for key in ("content", "refusal", "reasoning_content"):
                value = delta.get(key)
                if value is not None:
                    if not has_exact_type(value, str):
                        raise SecurityDenied("仅支持模型文本结果")
                    message[key] = (message.get(key) or "") + value
            for call in delta.get("tool_calls", []):
                slot = call.get("index")
                if not has_exact_type(slot, int) or not 0 <= slot < 64:
                    raise SecurityDenied("模型工具调用编号无效")
                calls = message.setdefault("tool_calls", [])
                if slot == len(calls):
                    calls.append(
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                if slot >= len(calls) or set(call) - {"index", "id", "type", "function"}:
                    raise SecurityDenied("模型工具调用顺序或字段无效")
                target = calls[slot]
                for key in ("id", "type"):
                    if key in call:
                        if target[key] and target[key] != call[key]:
                            raise SecurityDenied("模型工具调用身份改变")
                        target[key] = call[key]
                function = call.get("function", {})
                if set(function) - {"name", "arguments"}:
                    raise SecurityDenied("模型工具函数字段未覆盖")
                for key in ("name", "arguments"):
                    if key in function:
                        if not has_exact_type(function[key], str):
                            raise SecurityDenied("模型工具参数必须为文字")
                        target["function"][key] += function[key]
            if part.get("finish_reason") is not None:
                item["finish_reason"] = part["finish_reason"]
    result = {k: first[k] for k in ("id", "created", "model") if k in first}
    result.update(object="chat.completion", choices=[item for _, item in sorted(choices.items())])
    if usage is not None:
        result["usage"] = usage
    return result


def _controls(body):
    if (
        not has_exact_type(body, dict)
        or set(body)
        - {
            "id",
            "object",
            "created",
            "model",
            "choices",
            "usage",
            "system_fingerprint",
            "service_tier",
        }
        or (not body.get("choices"))
    ):
        raise SecurityDenied("模型结果格式未覆盖")
    elif len(body["choices"]) > 16:
        raise SecurityDenied("模型结果格式未覆盖")
    controls = {k: v for k, v in body.items() if k != "choices"}
    controls["choices"] = []
    for choice in body["choices"]:
        if set(choice) - {"index", "message", "finish_reason", "logprobs"} or choice.get(
            "logprobs"
        ):
            raise SecurityDenied("模型结果候选未覆盖")
        message = choice["message"]
        if (
            message.get("role") != "assistant"
            or set(message)
            - {"role", "content", "refusal", "reasoning_content", "tool_calls", "annotations"}
            or message.get("annotations")
        ):
            raise SecurityDenied("模型结果类型或结束状态未覆盖")
        elif choice.get("finish_reason") not in {"stop", "length", "tool_calls", "content_filter"}:
            raise SecurityDenied("模型结果类型或结束状态未覆盖")
        m = dict(message)
        for key in ("content", "refusal", "reasoning_content"):
            if message.get(key) is not None and not has_exact_type(message[key], str):
                raise SecurityDenied("模型结果必须为文本")
            if key in m:
                m[key] = None
        if "tool_calls" in m:
            m["tool_calls"] = []
            for call in message["tool_calls"] or []:
                if (
                    set(call) != {"id", "type", "function"}
                    or call["type"] != "function"
                    or (not call["id"])
                ):
                    raise SecurityDenied("模型工具调用结构无效")
                elif (
                    set(call["function"]) != {"name", "arguments"}
                    or not call["function"]["name"]
                    or (not has_exact_type(call["function"]["arguments"], str))
                ):
                    raise SecurityDenied("模型工具调用结构无效")
                parse_json(call["function"]["arguments"])
                m["tool_calls"].append({**call, "function": {**call["function"], "arguments": ""}})
        controls["choices"].append({**choice, "message": m})
    return encode_payload(controls)


def protect_completion(adapter, operation, text, streaming):
    body = completion_from_sse(text) if streaming else parse_json(text)
    controls = _controls(body)
    # Arguments are a second JSON document. Decode standard JSON escapes before
    # inspection; otherwise SDK parsing could materialize sensitive text only
    # after the transport check (e.g. an ID encoded entirely as \\u0031...).
    for choice in body["choices"]:
        for call in choice["message"].get("tool_calls") or []:
            arguments = parse_json(call["function"]["arguments"])
            if not has_exact_type(arguments, dict):
                raise SecurityDenied("模型工具参数必须为 JSON 对象")
            call["function"]["arguments"] = encode_payload(arguments)
    selected = adapter.results.observe(
        operation.actor,
        hook=Hook.AFTER_LLM_CALL,
        producer=operation.target,
        operation_id=operation.operation_id,
        value=body,
    )
    if _controls(selected) != controls:
        raise SecurityDenied("结果脱敏改变模型控制字段")
    if not streaming:
        return encode_payload(selected).encode()
    adapter.results.release_stream(
        operation.actor,
        producer=operation.target,
        operation_id=operation.operation_id,
        value=selected,
    )
    chunks = []
    for choice in selected["choices"]:
        message = dict(choice["message"])
        if message.get("tool_calls"):
            message["tool_calls"] = [
                {"index": i, **call} for i, call in enumerate(message["tool_calls"])
            ]
        base = {k: selected[k] for k in ("id", "model", "created") if k in selected}
        base.update(
            object="chat.completion.chunk",
            choices=[
                {
                    "index": choice["index"],
                    "delta": message,
                    "finish_reason": None,
                }
            ],
        )
        chunks.append(base)
        chunks.append(
            {
                **base,
                "choices": [
                    {
                        "index": choice["index"],
                        "delta": {},
                        "finish_reason": choice["finish_reason"],
                    }
                ],
            }
        )
    if selected.get("usage") is not None:
        chunks.append({**base, "choices": [], "usage": selected["usage"]})
    return (
        "".join("data: " + encode_payload(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    ).encode()


def response_headers(response):
    # 不把 provider 的任意响应头带进框架 trace/日志。
    kind = response.headers.get("content-type", "application/json").split(";")[0].strip().lower()
    if kind not in {"application/json", "text/event-stream"}:
        raise SecurityDenied("模型结果内容类型未覆盖")
    headers = {"content-type": kind}
    retry = response.headers.get("retry-after", "")
    if retry.isdigit() and int(retry) <= 30:
        headers["retry-after"] = retry
    return headers


def safe_error():
    return b'{"error":{"message":"model request unavailable","type":"request_error"}}'
