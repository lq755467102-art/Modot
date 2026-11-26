#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型入口 —— 一个接口，两种实现

为什么要抽这一层
    一开始本机没有可用的模型入口（环境里只有桌面本地服务，`/v1/models` 返回
    `{"ret":1,"version":"3"}`，不是 OpenAI 兼容网关）。但"能不能调模型"不该
    阻挡"循环对不对" ⇒ 先用 `MockProvider` 把循环的全部机制验通，拿到 key 只换一行实现。
    后来两家 key 都到位，`OpenAICompatProvider` 成了主路径，`MockProvider` 退居测试。

两个实现
    MockProvider        按剧本走，确定性。用来验循环、终止条件、验证器。离线跑验收靠它。
    OpenAICompatProvider  任何 OpenAI 兼容端点（base_url + api_key 从环境读）。
                          已实测两家：DeepSeek deepseek-flash / 智谱 glm-5.3-flash。
"""
# 主要引用方包括 `subtasks.py` 和 `execution_policy.py`。
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

from . import netpath as _NP
from . import private_keys


class Interrupted(Exception):
    """被要求停下。**不是错误，是第五个出口（paused）。**"""


class ProviderError(RuntimeError):
    """模型接口报错，带**能不能换个模型再试**这个判断。

    ★ 为什么要单独一个类型（2026-10-26）：
      原来一律 `RuntimeError(一句话)`，于是"该换个模型"和"该去改凭据"
      长得一模一样 —— 两种处置完全不同，混在一起只能靠人读错误文本猜。
      它是 RuntimeError 的子类，所有 `except RuntimeError` 的老代码不受影响。

    kind 只有五种，对应五种处置：
      auth        凭据不对 → 换模型也没用，去改 key
      bad_request 请求格式错 → 换模型也没用，去改代码
      rate_limit  限流     → 等一会儿或换模型
      server      服务端 5xx → 换模型
      network     连不上/超时 → 换模型（同机同网，但换个端点可能通）
    """

    def __init__(self, msg: str, kind: str = "server", status: int = 0,
                 can_fallback: bool = True, detail: str = ""):
        super().__init__(msg)
        self.kind = kind
        self.status = status
        self.detail = detail
        # ★ 默认**可以**切换：默认值选错的方向要选"多试一次"，
        #   因为不切的结果是整轮失败，而多切一次最坏是多花一次调用。
        #   auth / bad_request 显式置 False。
        self.can_fallback = bool(can_fallback)


def _kind_of(code: int) -> tuple[str, bool]:
    """HTTP 码 → (kind, 能不能换个模型再试)。"""
    if code in (401, 403):
        return "auth", False
    if code == 402:
        return "balance", False
    if code == 429:
        return "rate_limit", True
    if 400 <= code < 500:
        return "bad_request", False      # 请求本身不对，换谁都没用
    return "server", True


def retryable(code: int) -> bool:
    """这个 HTTP 状态码值得重试吗？

    只认「会自己好的」：限流 / 服务端 5xx / 并发冲突 / 请求超时。
    4xx 里的语义错误（400 / 401 / 403 / 404 / 422）重试一百次也一样，
    而且每次都可能计费 —— 那种必须立刻停，把错误体带给人看。

    ★ 400 是最典型的"不该重试"：我们踩过的两次 400
      （`reasoning_content` 空串没回传、一条回复拆成两个 assistant 消息）
      都是消息格式问题，重试只会同样失败，还多花钱。
    """
    return code in (408, 409, 425, 429, 500, 502, 503, 504, 529)


def backoff(attempt: int, base: float = 1.0, cap: float = 8.0) -> float:
    """第 attempt 次（从 0 起）失败后等多久：1s → 2s → 4s → 8s 封顶。

    为什么不加随机抖动：我们是单机单任务，没有惊群问题；
    加抖动只会让"这次等了多久"更难解释。真要并发跑多个再考虑。
    """
    return min(base * (2 ** attempt), cap)


def _context_limit_error(error):
    if not isinstance(error,dict):return None
    code=str(error.get('code','')).lower()
    message=str(error.get('message',''))
    if code not in {'context_length_exceeded','context_window_exceeded','input_too_long','prompt_too_long'} and not re.search(
            r'maximum context length|context (?:length|window).*(?:exceed|too (?:long|large))|超过.*上下文|上下文.*超',message,re.I):return None
    def number(keys,pattern):
        for key in keys:
            value=error.get(key)
            if type(value) is int and value>0:return value
        match=re.search(pattern,message,re.I)
        return int(match[1]) if match else None
    return dict(kind='api_context_limit',limit=number(('context_window','max_context_tokens','context_length'),r'maximum context length is\s*(\d+)'),
                inputTokens=number(('input_tokens','prompt_tokens'),r'(\d+)\s+in (?:the )?(?:messages|prompt|input)'))


def _image_failure(error, status=None):
    if not isinstance(error, dict):
        return None
    detail = " ".join(str(error.get(key) or "")[:1000] for key in ("code", "type", "param", "message")).lower()
    if not re.search(r"image|vision|multi.?modal|图片|图像|视觉", detail):
        return None
    if not re.search(r"unsupported|not[_ -]support|doesn['’]t support|invalid|text.only|不支持|不接受|无效|无法", detail):
        return None
    return ProviderError("模型接口拒收图片输入。可以切换模型后重新附上图片发送。",
                         kind="image_input", status=status, can_fallback=False)


def _http_failure(error):
    kind,fallback=_kind_of(error.code)
    if retryable(error.code) and kind=='bad_request':kind='server'
    details=None
    media_error=None
    if error.code in {400,413,415,422}:
        try:
            detail=json.loads(error.read(65536)).get('error')
            details=_context_limit_error(detail)
            if not details:media_error=_image_failure(detail,error.code)
        except (ValueError,AttributeError,OSError):pass
    result=media_error or ProviderError('模型接口 HTTP %s'%error.code,kind='context_limit' if details else kind,
                         status=error.code,can_fallback=False if details else fallback)
    if details:result.context_limit=details
    try:result.retry_after=max(0,float(error.headers.get('Retry-After',0)))
    except (TypeError,ValueError,AttributeError):pass
    error.close()
    return result


def _recover_context(provider,body,error,attempt,should_stop):
    details=getattr(error,'context_limit',None)
    if not details:return None
    from .context_inspection import active_request
    from .context_manager import prepare,capacity_for,bind_capacity,model_window,RequestBody
    owner=active_request.get()
    if not owner:return None
    window=details.get('limit');tokens=details.get('inputTokens')
    effective = capacity_for(owner[0], provider)
    current_window = (effective if getattr(owner[0], 'context_model_limits', None) is None
                      else model_window(owner[0], provider))
    if window and (current_window is None or window < current_window):
        bind_capacity(owner[0],provider,window)
    effective=capacity_for(owner[0],provider)
    if getattr(body, 'budget_optional_retry', False):
        from .input_budget import reject_optional_retry
        reject_optional_retry(owner[0], provider, owner[1], body, effective, details)
    # A combined input/output rejection need not discard any input at all.
    output_key='max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
    if window and tokens and tokens<window and (not effective or tokens<effective) and type(body.get(output_key)) is int and body[output_key]>window-tokens:
        revised=RequestBody(dict(body),getattr(body,'parts',{}));revised[output_key]=window-tokens
        if getattr(body, 'input_budget', None):
            from .input_budget import report_after_rejection
            revised.budget_deferred = body.input_budget["deferred"]
            revised.input_budget = report_after_rejection(owner[0], provider, revised, effective)
        owner[0].emit('context_output_adjustment',role=owner[1],limit=window,inputTokens=tokens,maxOutput=window-tokens)
        return revised
    # A summary is an auxiliary prefix, not the live history. Its caller owns
    # shortening that prefix; prepare() intentionally cannot compact recursively.
    if getattr(owner[0],'context_summary_active',False):return None
    if attempt>=3 or not getattr(owner[0],'context_compaction_enabled',True):return None
    # Rebuild each attempt from the original working history. Compacting the
    # previous candidate would make adoption compare against the wrong source.
    pending=getattr(owner[0],'context_adoptions',{}).get(owner[1],{})
    source=pending.get('original',body)
    source=RequestBody(dict(source),getattr(source,'parts',{}))
    for key in ('max_tokens','max_completion_tokens'):
        if key in body:source[key]=body[key]
    revised=prepare(provider,source,should_stop,limit_evidence=details,keep_tools=(6,3,1)[attempt])
    from .context_manager import fingerprint
    if getattr(revised, 'budget_optional_retry', False) and len(json.dumps(revised)) >= len(json.dumps(body)):
        from .input_budget import block
        block(owner[0], owner[1], revised.input_budget, "provider", details)
    if fingerprint(revised)==fingerprint(body):return None
    adopted=getattr(owner[0],'context_adoptions',{}).get(owner[1])
    if adopted and pending.get('previousToolReceipts'):
        adopted['previousToolReceipts']={**adopted.get('previousToolReceipts',{}),**pending['previousToolReceipts']}
    return revised


class Msg:
    """一条消息。够用就好，不做多模态。"""
    def __init__(self, role: str, content: str = "", tool_calls=None,
                 tool_call_id: str = "", reasoning: str = "", context_parts=(), image_action=None):
        self.role = role
        self.content = private_keys.clean(content)
        self.tool_calls = private_keys.clean(tool_calls) if tool_calls else []
        self.tool_call_id = private_keys.text(tool_call_id)
        # ★ DeepSeek 思考模式要求把 reasoning_content 原样回传，否则下一轮 400。
        #   （实测错误：The `reasoning_content` in the thinking mode must be passed back）
        self.reasoning = private_keys.text(reasoning)
        self.context_parts = [(kind, private_keys.text(part)) for kind, part in context_parts] if context_parts else context_parts
        if self.content != content and self.context_parts and "".join(part for _, part in self.context_parts) != self.content:
            # A marker split across two contextual spans has only one safe projection.
            self.context_parts = ()
        self.image_action = image_action

    def to_dict(self) -> dict:
        # ★ 带 tool_calls 的 assistant 消息，content 必须是 null 而不是 ""，
        #   否则 DeepSeek 报 400（实测）。这是 OpenAI 兼容层最常见的坑之一。
        if self.tool_calls:
            d = {"role": self.role, "content": self.content or None, "tool_calls": self.tool_calls}
            # ★★ 坑中坑：DeepSeek 思考模式要求 `reasoning_content` **字段必须存在**，
            #    哪怕它是空字符串。原来是 `if self.reasoning:` 才加 —— 空串被当"没有"，
            #    字段整个没发出去，报了个看起来毫不相干的
            #    「The reasoning_content in the thinking mode must be passed back」。
            #    ⇒ **"空"和"不存在"不是一回事**，这里必须显式发空串。
            d["reasoning_content"] = self.reasoning or ""
        else:
            d = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.reasoning and not self.tool_calls:
            d["reasoning_content"] = self.reasoning
        return d

    def dump(self) -> dict:
        """存档用（续跑要用它，**不能用 `to_dict()`**）。

        区别只有一个，但是致命的：`to_dict()` 是**发给模型**的格式，
        会按规则裁掉不需要回传的字段；而存档要的是**能原样还原**的格式。
        实测过的坑：DeepSeek 思考模式要求 `reasoning_content` 必须回传，
        只存 `to_dict()` 的话续跑时那条 assistant 消息没有 reasoning，
        报「reasoning_content must be passed back」，而错误信息看起来跟存档毫无关系。
        """
        from .context_manager import content_spans
        spans = content_spans(self.content, self.context_parts)
        return {"role": self.role,
                "content": self.content,
                "tool_calls": self.tool_calls or [],
                "tool_call_id": self.tool_call_id or "",
                "reasoning": self.reasoning or "",
                **({"context_spans": spans} if spans else {}),
                **({"image_action": self.image_action} if self.image_action else {})}

    @classmethod
    def load(cls, d: dict) -> "Msg":
        content = d.get("content") or ""
        parts = d.get("context_parts") or ()
        if isinstance(content, str) and d.get("context_spans"):
            parts = [(kind, content[start:end]) for kind, start, end in d["context_spans"]
                     if 0 <= start < end <= len(content)]
        return cls(d.get("role") or "user",
                   content=content,
                   tool_calls=d.get("tool_calls") or [],
                   tool_call_id=d.get("tool_call_id") or "",
                   reasoning=d.get("reasoning") or "",
                   context_parts=parts, image_action=d.get("image_action"))


class Reply:
    """模型的一次回应。要么要调工具，要么给最终话。"""
    def __init__(self, text: str = "", tool_calls=None, reasoning: str = "",
                 usage: dict | None = None, finish_reason: str = ""):
        self.text = private_keys.text(text)
        self.tool_calls = []
        for call in tool_calls or []:
            safe = private_keys.clean(call)
            if safe != call:
                safe["_bad_args"] = "工具参数包含私钥，未执行。请在聊天的 SSH 身份入口操作。"
            self.tool_calls.append(safe)
        self.reasoning = private_keys.text(reasoning)
        # ★ 接口返回的真实用量。**"上下文用了多少"只能从这里拿** ——
        #   估一个数字填进报告，是这套东西里最不该有的东西：
        #   它看起来像测量结果，实际是凭印象写的，而且没人能复核。
        self.usage = usage or {}
        # ★ 为什么要记：`finish_reason == "length"` = 预算被用完，输出可能是残的。
        #   不显形的话，"写了一半"会被当成"写完了"。
        self.finish_reason = finish_reason
        self.input_image_urls = None
        self.input_image_actions = None

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


def assistant_tool_calls(calls, turn: int = 0) -> list[dict]:
    """把 `Reply.tool_calls`（扁平：id/name/args）转成**要回传的** OpenAI 结构。

    ★ 为什么要单独一个函数（2026-10-29）：这个转换原来只写在 `loop/loop.py` 里，
      而 `plan.py`（规划者的循环）自己又写了一遍 —— 写漏了 `type: "function"`
      和 `function` 那层包装，于是第二次调模型时 DeepSeek 回
      422 `messages[2]: missing field 'type'`。
      那个报错**完全指不出**「你的 tool_calls 结构不对」——
      它说的是消息缺字段，看起来像消息格式的问题。

    ★ 结论：**这个转换只能有一处**。两个调用方都从这里拿，谁也别自己拼。
    """
    out = []
    for i, c in enumerate(calls or []):
        if not isinstance(c, dict):
            continue
        _args = c.get("args")
        out.append({
            "id": c.get("id") or "c%d_%d" % (turn, i),
            "type": "function",
            "function": {
                "name": c.get("name"),
                "arguments": (_args if isinstance(_args, str)
                              else json.dumps(_args or {}, ensure_ascii=False)),
            },
        })
    return out


# ───────────────── 流式：解析 + 聚合（纯函数，可离线测） ─────────────────
# ★ 为什么把这两件事从网络代码里拆出来
#   流式的坑（tool_calls 分片拼错、reasoning 丢了、usage 漏了）在真调用里
#   表现为"偶尔少一点内容"，极难复现。拆成吃字符串的纯函数，就能拿一段固定
#   SSE 文本反复验 —— 一贯的做法：把"说不清对错"变成"能跑两遍比结果"。

def sse_payload(raw: bytes):
    """一行 SSE → dict（一个 chunk）/ "DONE" / None（空行、注释、不认识的行）。

    ★ 不认识的行走 None 而不是抛错：真流里有心跳注释（`: keep-alive`）和
      服务端自加的字段。一行看不懂就整轮失败，是最不该的失败方式。
    """
    try:
        s = raw.decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not s or s.startswith(":"):
        return None
    if not s.startswith("data:"):
        return None
    payload = s[5:].strip()
    if payload == "[DONE]":
        return "DONE"
    if not payload:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


class StreamAcc:
    """把一串 chunk 攒成一个 Reply。

    ★ tool_calls 在流里是**分片**的：id 可能只来一次，name 可能只来一次，
      arguments 是一段一段拼的（`{"pa` / `th":` / ` "a.md"}`）。
      按 index 归并，不能按出现顺序 —— 同一轮可以有多个工具调用同时流。
      这里拼错的表现是"参数少一截"，而调用照常发出，报错在很远的地方。
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._text: list[str] = []
        self._reason: list[str] = []
        self._calls: dict[int, dict] = {}
        self._finish = ""
        self._usage: dict = {}
        self.done = False

    def empty(self) -> bool:
        return not (self._text or self._reason or self._calls)

    def feed(self, obj: dict) -> dict:
        """喂一个 chunk，返回这批新增（给 on_delta 用，不重复旧内容）。"""
        if not isinstance(obj, dict):
            raise ProviderError('模型流格式错误', kind='incomplete_stream')
        if obj.get('error'):
            error = obj['error'] if isinstance(obj['error'], dict) else {}
            if details:=_context_limit_error(error):
                failure=ProviderError('上游明确返回上下文超限',kind='context_limit',can_fallback=False)
                failure.context_limit=details
                raise failure
            if failure:=_image_failure(error):
                raise failure
            tag = str(error.get('type', '')) + ' ' + str(error.get('code', ''))
            kind = ('auth' if any(s in tag.lower() for s in ('auth', 'api_key')) else
                    'bad_request' if any(s in tag.lower() for s in ('balance', 'quota', 'invalid', 'insufficient')) else
                    'rate_limit' if 'rate' in tag.lower() else 'server')
            raise ProviderError('模型流返回上游错误（%s）' % kind, kind=kind,
                                can_fallback=kind in {'server','rate_limit'})
        add = {"text": "", "reasoning": "", "tool": ""}
        if obj.get("usage"):
            self._usage = obj["usage"]
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            t = d.get("content") or ""
            if t:
                self._text.append(t)
                add["text"] = t
            # reasoning_content 是 DeepSeek 的字段名，有的服务端叫 reasoning
            r = d.get("reasoning_content") or d.get("reasoning") or ""
            if r:
                self._reason.append(r)
                add["reasoning"] = r
            for tc in d.get("tool_calls") or []:
                i = tc.get("index")
                if i is None:
                    i = 0
                slot = self._calls.setdefault(i, {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                    add["tool"] = slot["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
            if ch.get("finish_reason"):
                self._finish = ch["finish_reason"]
        return add

    def reply(self) -> Reply:
        calls = []
        for i in sorted(self._calls):
            s = self._calls[i]
            raw = s["args"].strip()
            args = {}
            bad = ""
            if raw:
                try:
                    args = json.loads(raw)
                except Exception as e:
                    # ★ 拼坏的参数不能悄悄变成 {} —— 那会让工具收到空参数还照跑，
                    #   报错在很远的地方。留个记号，让上层能看见。
                    bad = "参数不是合法 JSON（%s）：%s" % (e, raw[:120])
                    args = {"_raw": raw}
            calls.append({"id": s["id"] or ("call_%d" % i), "name": s["name"], "args": args,
                          **({"_bad_args": bad} if bad else {})})
        return Reply(text="".join(self._text), tool_calls=calls,
                     reasoning="".join(self._reason), usage=self._usage,
                     finish_reason=self._finish)


def stream_reply(lines, on_delta=None, acc=None, *, strict=False, observation=None) -> Reply:
    """吃一串 SSE 行，吐一个 Reply。on_delta(kind, text) 每批新增回调一次。

    ★ on_delta 在主线程被调用：调用方（CLI）要直接往 stdout 写，
      多线程同时写 stdout 会交错。行由 _iter_lines 运回主线程。

    ★ `acc` 可以由调用方传进来复用：流式中途断了之后，外层要判断
      「已经收到过内容没有」再决定能不能重试 —— 那就得看同一个 acc。
    """
    acc = acc or StreamAcc()
    filters = {kind: private_keys.PrivateKeyFilter() for kind in ("text", "reasoning")}
    for raw in lines:
        obj = sse_payload(raw)
        if observation is not None:
            observation.line(raw, obj)
        if obj is None:
            continue
        if obj == "DONE":
            acc.done = True
            break
        add = acc.feed(obj)
        if on_delta:
            for kind in ("text", "reasoning", "tool"):
                if add[kind]:
                    safe = filters[kind].feed(add[kind]) if kind in filters else add[kind]
                    if safe:
                        on_delta(kind, safe)
    if on_delta:
        for kind, guard in filters.items():
            if tail := guard.finish():
                on_delta(kind, tail)
    reply = acc.reply()
    if observation is not None:
        observation.parsed(reply)
    if strict:
        if reply.finish_reason == 'length' and (reply.tool_calls or not reply.text):
            raise ProviderError('模型输出预算不足，未执行残缺调用', kind='budget', can_fallback=False)
        if not (reply.text.strip() or reply.tool_calls):
            raise ProviderError('上游未返回有效模型响应', kind='empty_response')
        if not (acc.done or reply.finish_reason):
            raise ProviderError('模型响应在完成之前断开', kind='incomplete_stream')
        if any(call.get('_bad_args') or not call.get('name') for call in reply.tool_calls):
            raise ProviderError('模型工具参数不完整，未执行工具', kind='incomplete_stream')
    return reply


class Provider:
    name = "base"
    # ★ 能不能流式，由 provider 自己声明 —— 不要让调用方去 hasattr 猜。
    #   基类默认 False：加了流式是能力，不是义务，Mock 就没必要实现。
    supports_stream = False

    def chat(self, messages: list[Msg], tools: list[dict], should_stop=None) -> Reply:
        raise NotImplementedError


class MockProvider(Provider):
    """按剧本回放。剧本里每一步是：

        {"tool": "run_check", "args": {...}}      要调工具
        {"text": "我做完了"}                       要收工（**验证器说了算**）

    剧本用完还在被调用 ⇒ 抛错，避免"循环其实没停"被掩盖。
    """
    name = "mock"
    # ★ 刻意**没有** should_stop 实例属性：它曾经是实例属性，
    #   同一进程里跑父子两个 agent 时会互相覆盖（subagent-probe.py 实测过）。
    #   现在是 chat() 的参数，一次调用一份，谁也不碰谁。

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.i = 0
        self.seen: list[dict] = []

    def chat(self, messages, tools, should_stop=None) -> Reply:
        # ★ texts 要记下来：否则"每轮都注入了总纲"这件事，测试里没法核 ——
        #   只能核"注入了几次"，核不了"注入在第几条、是不是每轮都有"。
        self.seen.append({"n_msgs": len(messages), "n_tools": len(tools),
                          "texts": [getattr(m, "content", "") or "" for m in messages]})
        if self.i >= len(self.script):
            raise RuntimeError("剧本用完了但循环还在要下一轮 —— 这本身就说明终止条件没生效")
        step = self.script[self.i]
        self.i += 1
        if "tool" in step:
            return Reply(tool_calls=[{"id": "call_%d" % self.i,
                                      "name": step["tool"],
                                      "args": step.get("args") or {}}])
        return Reply(text=step.get("text", ""), finish_reason=step.get("finish_reason", ""))


class OpenAICompatProvider(Provider):
    """OpenAI 兼容端点（已用 DeepSeek 实测）。

    凭据读两处，**优先环境变量**：
      1. `HARNESS_BASE_URL` / `HARNESS_API_KEY` / `HARNESS_MODEL`
      2. `~/.workbuddy/harness/credentials.env`（**故意放在仓库外，进不了 git**）

    ★ 为什么放仓库外：key 一旦进过 git 历史，删掉也没用 —— 得换 key。
      这个文件 600 权限，且 `.gitignore` 里也挡了一道。
    """
    name = "openai-compat"
    # ★ 声明能力，别让调用方 hasattr 猜。真调用的服务端不一定支持
    #   stream_options，但「支持流式」这件事本身是 OpenAI 兼容端点的通例。
    supports_stream = True
    # ★ 允许覆盖：一是测试要用（不能拿真凭据当试验田），
    #   二是同一台机器上跑两套环境（比如一套走公司网关、一套走个人 key）。
    CRED_PATH = (os.environ.get("HARNESS_CREDS")
                 or os.path.expanduser("~/.workbuddy/harness/credentials.env"))

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 profile: str = "HARNESS", max_tokens=None, *, request_guard=None):
        """`profile` 是环境变量/凭据文件里的前缀。

        ★ 为什么要这个参数：**自我和本我必须能用两家的模型**。
          同厂同模型审自己，能多买到的独立性接近于零
          （实测数据：两个同族裁判互相一致率 94%，若彼此独立应该是 74.4%
           ⇒ 只多买 6 个点，剩下约 20 点是共同错误）。
          所以本我这份凭据用另一个前缀 `HARNESS_REVIEW`，两个入口互不干扰。
        """
        self.request_guard = request_guard
        cred = self._read_creds()
        pre = (profile or "HARNESS").rstrip("_") + "_"
        self.profile = (profile or "HARNESS").rstrip("_")
        # ★ 模型档案（2026-10-26）：界面上「自我 / 本我 各用哪一份」存在
        #   `~/.workbuddy/harness/models.json`。这一层只做一件事：**取值顺序**。
        #     显式传参 > 环境变量 > 档案里选中的那份 > credentials.env
        #   ★ 为什么环境变量排在档案前面：`HARNESS_MODEL=x` 是命令行上当场做的决定。
        #   ★ 为什么档案排在 credentials.env 前面：那个文件是"上一代唯一的配置"，
        #     不动它就没法让界面上的选择生效 —— 而"选了不生效"比没有选择更坏。
        #   ★ 档案缺失/读坏都不抛：模型入口建不起来会连带整轮任务失败，
        #     那不该由"一个 JSON 坏了"引起。
        _m: dict = {}
        try:
            from . import models as _M
            # ★ 按 profile 映射**角色**（2026-10-29 用户：「规划者的模型选择不持久化」）。
            #   原来只有两路：`REVIEW` 结尾 → review，其余 → main。
            #   于是 `profile="plan"` 也被当成 main —— 界面上给规划者选的模型
            #   存进了 `models.json` 的 `plan` 角色，而这里**从来不读它**。
            #   ⇒ 不是"没保存"，是**保存了但没人读**。
            _prof = (self.profile or "").upper()
            _role = "main"
            if _prof.endswith("REVIEW"):
                _role = "review"
            elif _prof.endswith("PLAN"):
                _role = "plan"
            elif _prof == _M.ROLES["vision"]:
                _role = "vision"
            _m = _M.resolve(_role)
            # ★ 规划者没配自己的模型时，**借用执行者的**（2026-10-29 用户原话：
            #   「能把执行者的 key 暂时借用过去吗」）。
            #   没有这层兜底的话，`profile="plan"` 会因为 plan 角色是空的而直接报错，
            #   界面上表现为"规划者根本跑不起来"。
            if _role == "plan" and not _m.get("model"):
                _m = _M.resolve("main")
        except Exception:
            _m = {}
        self.base_url = (base_url or os.environ.get(pre + "BASE_URL")
                         or _m.get("base_url")
                         or cred.get(pre + "BASE_URL", "")).rstrip("/")
        self.api_key = (api_key or os.environ.get(pre + "API_KEY")
                        or _m.get("api_key")
                        or cred.get(pre + "API_KEY", ""))
        self.model = (model or os.environ.get(pre + "MODEL")
                      or _m.get("model")
                      or cred.get(pre + "MODEL", ""))
        # 从哪儿来的（诊断包和界面都要能回答"现在生效的是哪一份"）
        self.model_source = "explicit" if model else "env" if os.environ.get(pre+"MODEL") else _m.get("source") or "none"
        try:self.request_context_limit=max(0,int(os.environ.get(pre+'CTX') or 0))
        except (ValueError,TypeError):self.request_context_limit=0
        self.model_name_cn = _m.get("name") or ""
        # 思考档（实测智谱 glm-5.3-flash 默认思考很重：能把几千 token 全花在推理上）
        self.reasoning_effort = (os.environ.get(pre + "REASONING")
                                 or cred.get(pre + "REASONING", ""))
        # Modot's default output budget is independent of input context.
        from .models import output_limit
        matched_profile = (_m if self.model == _m.get("model")
                           and self.base_url == str(_m.get("base_url") or "").rstrip("/") else {})
        self.model_output_limit = matched_profile.get("max_output_tokens")
        self.max_tokens = 128000
        self.max_tokens_source = "service_default"
        # Explicit 0 means omit the field and accept the upstream default.
        for source, value in (("credentials", cred.get(pre + "MAXTOK")),
                              ("models", matched_profile.get("max_tokens")),
                              ("env", os.environ.get(pre + "MAXTOK")),
                              ("explicit", max_tokens)):
            if value is None or value == "":
                continue
            try:
                parsed = output_limit(value)
            except ValueError:
                if source == "explicit":
                    raise
                continue
            self.max_tokens, self.max_tokens_source = parsed, source
        if self.model_output_limit and self.max_tokens > self.model_output_limit:
            self.max_tokens = self.model_output_limit
        # ★ 超时也是"自己套的绳子"。原来硬写 120 秒 ——
        #   实测深度思考档一次要 >120 秒，于是被斩断，表现为"什么都没输出"。
        #   默认放到 300，可配。
        try:
            self.timeout = int(os.environ.get(pre + "TIMEOUT")
                               or cred.get(pre + "TIMEOUT") or 300)
        except ValueError:
            self.timeout = 300
        # ★ 重试次数。原来是 0 —— 一次 429 或一次网络抖动，整轮任务废掉，钱还不退。
        #   默认 2（最多试 3 次）。只重试**可重试的**：限流 / 服务端 / 超时；
        #   4xx 里的语义错误（比如消息格式不对）重试一百次也一样，白花钱。
        try:
            self.retries = int(os.environ.get(pre + "RETRIES")
                               or cred.get(pre + "RETRIES") or 2)
        except ValueError:
            self.retries = 2
        try:
            self.recovery_seconds = max(1, int(os.environ.get(pre + 'RECOVERY_SECONDS') or 60))
        except ValueError:
            self.recovery_seconds = 60
        # ★★★ 网络出口**自己定**，不再继承系统/环境里的代理（2026-10-27 用户「还是不对」）。
        #   根因不是模型，也不是抖动：界面里的后端是 Electron 起的子进程，
        #   它继承的是**主进程启动那一刻**的环境变量；而本机的代理软件
        #   （`HTTP_PROXY=http://127.0.0.1:<随机端口>`）**每次重启都换端口**。
        #   ⇒ 界面一直在连一个早就关掉的端口，现象是**稳定复现**的 10054；
        #     而同一个请求在命令行里（新的环境）却是 200 —— 这就是"命令行好好的、
        #     界面一直坏"的全部原因。
        #   ⇒ 默认**直连**（模型 API 本来就在公网，代理只是多一个会坏的中转，
        #     不该由它决定 harness 能不能用）；真要走代理，显式设 `HARNESS_PROXY`。
        #     环境里的 HTTP_PROXY **不再被采纳** —— 它的生命周期不归我们管。
        #   实现在 `loop/netpath.py`（一处决定，provider / models / mcp 共用）。
        from . import netpath as _NP
        self.proxy = _NP.proxy_setting()
        self._opener = _NP.opener(self.proxy)

        if not (self.base_url and self.api_key and self.model):
            raise RuntimeError(
                "缺配置：需要 %sBASE_URL / %sAPI_KEY / %sMODEL，"
                "或把它们写进 %s" % (pre, pre, pre, self.CRED_PATH))

    @property
    def label(self) -> str:
        """给报告用的一句话标识 —— **页面必须写清是谁在判**。"""
        return "%s（%s）" % (self.model, self.profile.lower())

    @classmethod
    def _read_creds(cls) -> dict:
        p = cls.CRED_PATH
        if not os.path.isfile(p):
            return {}
        out = {}
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
        return out

    # 同上：停止回调走 chat() 的参数，不做实例属性。

    def _body(self, messages: list[Msg], tools: list[dict], stream: bool = False,
              max_tokens=None) -> dict:
        """请求体。**流式和非流式共用一处** —— 各写一套的话，以后加字段会漏一边，
        而漏掉的那边在真调用里表现成"偶尔少个参数"，很难归因。
        """
        from .context_manager import RequestBody
        from .context_inspection import active_request
        from .model_catalog import model_source
        owner = active_request.get()
        if owner and not getattr(owner[0],'context_summary_active',False):
            from .working_context import append_updates
            append_updates(*owner, messages)
        wire_messages = [private_keys.clean(m.to_dict()) for m in messages]
        service = model_source(self.base_url) or {}
        if (service.get("id") == "deepseek" and self.model in {"deepseek-flash", "deepseek-v4-pro"}
                and self.reasoning_effort != "none"):
            # Injected working notes retain their assistant role. The thinking
            # endpoint also requires the field on those non-tool messages.
            for message in wire_messages:
                if message["role"] == "assistant":
                    message.setdefault("reasoning_content", "")
        parts = {i: m.context_parts for i, m in enumerate(messages) if getattr(m, "context_parts", ())}
        for i, m in enumerate(messages):
            marker = getattr(m, "image_action", None)
            if marker:
                parts[i] = (*parts.get(i, ()), ("imageAction", marker["id"]))
        body = RequestBody({
            "model": self.model,
            "messages": wire_messages,
        }, parts)
        output = self.max_tokens
        if max_tokens is not None:
            from .models import output_limit
            output = output_limit(max_tokens)
            capability = getattr(self, "model_output_limit", None)
            if capability and output > capability:
                output = capability
        if output > 0:
            body[service.get("completion_limit_field", "max_tokens")] = output
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if stream:
            body["stream"] = True
            # ★ 不写这句，流式下最后一个 chunk 不带 usage，账本就只能靠估。
            #   DeepSeek / OpenAI 都认；真遇到不认的服务端会 400 ——
            #   那时把这一行去掉，代价是流式下没有精确用量。
            body["stream_options"] = {"include_usage": True}
        return body

    def _request(self, body: dict) -> urllib.request.Request:
        guard = getattr(self, "request_guard", None)
        if guard is not None:
            guard()
        from .completion_review import before_request
        before_request()
        from .context_inspection import capture
        reference = capture(self, body)
        from .model_catalog import request_headers
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **request_headers(self.base_url, self.api_key)},
        )
        req._context_observation = reference
        return req

    def _once(self, req, should_stop=None):
        """发一次请求，**但主线程不闷在 socket 里**。

        为什么必须放线程：实测过 —— 不放线程时，信号到达后要等这次 HTTP 读完
        才被处理，延迟 4.13 秒（= 那次调用卡住的剩余时间，最坏能到 300 秒）。
        也就是说"能暂停"当时只是"能等"。
        现在主线程每 0.1 秒看一眼 should_stop，收到就走，不等它返回。

        ★ 代价要说清：**已经发出去的请求照样计费**，这里省的是"人的等待"，
          不是"这一次的钱"。所以中断之后那一轮的 token 仍然要算。
        """
        from .provider_recovery import close_response
        box = {}
        cancelled = threading.Event()
        response = []

        def work():
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    response.append(r)
                    if cancelled.is_set():
                        return
                    payload = r.read()
                    observation = getattr(req, '_response_observation', None)
                    try:
                        box["d"] = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        if observation is not None:
                            observation.json_bytes(len(payload), malformed=True)
                        raise
                    if observation is not None:
                        observation.json_bytes(len(payload))
            except BaseException as e:          # 包括超时、连不上、HTTP 错误
                box["e"] = e

        th = threading.Thread(target=work, daemon=True)
        th.start()
        try:
            while th.is_alive():
                if should_stop is not None and should_stop():
                    raise Interrupted("收到停止请求，放弃这次模型调用")
                th.join(0.1)
        finally:
            cancelled.set()
            if response:
                close_response(response[0])
        if "e" in box:
            raise box["e"]
        return box["d"]

    def chat(self, messages: list[Msg], tools: list[dict], should_stop=None,
             *, max_tokens=None) -> Reply:
        """Non-stream recovery has the same boundaries as streaming recovery."""
        from .context_manager import prepare
        from .provider_recovery import Recovery
        from .model_observation import begin
        overrides = {} if max_tokens is None else dict(max_tokens=max_tokens)
        body = prepare(self, self._body(messages, tools, **overrides), should_stop)
        context_attempt=0
        recovery = Recovery(self, should_stop)
        while True:
            recovery.check()
            observation = None
            try:
                req = self._request(body)
                observation = begin(req, self, recovery, 'json')
                d = self._once(req, recovery.check)
                observation.received(d)
                if not isinstance(d, dict):
                    raise ProviderError('模型响应格式错误', kind='incomplete_stream')
                if d.get('error'):
                    StreamAcc().feed(d)
                choices = d.get('choices') or []
                if not choices or not choices[0].get('message'):
                    raise ProviderError('上游未返回有效模型响应', kind='empty_response')
                m = choices[0]['message']
                if choices[0].get('finish_reason') == 'length' and (m.get('tool_calls') or not m.get('content')):
                    raise ProviderError('模型输出预算不足，未执行残缺调用', kind='budget', can_fallback=False)
                if not ((m.get('content') or '').strip() or m.get('tool_calls')):
                    raise ProviderError('上游返回空内容', kind='empty_response')
                for call in m.get('tool_calls') or []:
                    function = call.get('function') or {}
                    if not function.get('name') or not isinstance(json.loads(function.get('arguments') or '{}'), dict):
                        raise ProviderError('模型工具参数不完整，未执行工具', kind='incomplete_stream')
                if m.get('tool_calls') and choices[0].get('finish_reason') == 'length':
                    raise ProviderError('模型输出预算不足，未执行残缺调用', kind='budget', can_fallback=False)
                recovery.check()
                recovery.success()
                break
            except Interrupted:
                if observation is not None:
                    observation.finish(error=Interrupted())
                recovery.emit('cancelled')
                raise
            except urllib.error.HTTPError as e:
                error=_http_failure(e)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                error = ProviderError('模型连接中断或超时', kind='network')
            except (ValueError, KeyError, TypeError):
                error = ProviderError('模型响应格式错误', kind='incomplete_stream')
            except ProviderError as e:
                error = e
            except BaseException as e:
                if observation is not None:
                    observation.finish(error=e)
                raise
            if observation is not None:
                observation.finish(error=error)
            revised=_recover_context(self,body,error,context_attempt,recovery.check)
            if revised is not None:
                body=revised;context_attempt+=1;continue
            recovery.retry(error)
        m = (d.get("choices") or [{}])[0].get("message") or {}
        calls = [{"id": c.get("id") or "call_%d" % i,
                  "name": (c.get("function") or {}).get("name") or "",
                  "args": json.loads((c.get("function") or {}).get("arguments") or "{}")}
                 for i, c in enumerate(m.get("tool_calls") or [])]
        _fr = (d.get("choices") or [{}])[0].get("finish_reason") or ""
        _txt = m.get("content") or ""
        _rsn = m.get("reasoning_content") or ""
        # ★★★ 思考把预算吃光、正文一个字没剩 ⇒ **报清楚**，别静默返回空串（2026-10-27）。
        #   这就是用户看到的「它直接不说话不输出了」：事件流里是
        #   `llm content=0 reasoning=2083 finish_reason=length` —— 模型一直在想，
        #   token 用完了，正文还没开始写。
        #   静默返回空串的话，上层只会得到"这一轮它没说话"，而真正的原因是**预算**。
        #   ★ 有 tool_calls 时不算错：模型只调工具、不说话是正常的。
        if not _txt.strip() and not calls and _fr == "length":
            _det = ((d.get("usage") or {}).get("completion_tokens_details") or {})
            _rt = _det.get("reasoning_tokens")
            raise ProviderError(
                "这次输出被预算截断了：思考花了 %s token（上限 %d），正文一个字没剩下。"
                "调大 max_tokens 或换一个思考更省的模型。"
                % (_rt if _rt else "大量", self.max_tokens or 0),
                kind="budget", can_fallback=True)
        from .context_manager import record_usage
        record_usage(self,body,d.get('usage') or {})
        result = Reply(text=_txt, tool_calls=calls,
                     reasoning=_rsn,
                     usage=d.get("usage") or {},
                     finish_reason=_fr)
        from .image_evidence import request_image_actions, request_image_urls
        result.input_image_actions = request_image_actions(body)
        result.input_image_urls = request_image_urls(body["messages"])
        observation.finish(reply=result)
        return result

    # ───────────────────── 流式 ─────────────────────

    def _iter_lines(self, req, should_stop=None):
        """把 SSE 行运回主线程。**主线程不闷在 readline 上。**

        ★ 和非流式的 `_once` 一样必须放线程：`readline()` 一样会阻塞。
          不放线程的话，"能中断"就退化成"能等这次读完"（实测过 4.13 秒）。
        ★ 用 0.1 秒轮询而不是死等：网络静默时（模型在思考、还没吐字）
          仍然要能停。流式的"到达频繁"不能当心跳用。
        """
        from .provider_recovery import close_response
        q: queue.Queue = queue.Queue()
        cancelled = threading.Event()
        response = []

        def work():
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    response.append(r)
                    if cancelled.is_set():
                        return
                    for raw in r:
                        if cancelled.is_set():
                            return
                        q.put(("line", raw))
            except BaseException as e:
                q.put(("err", e))
            finally:
                q.put(("done", None))

        threading.Thread(target=work, daemon=True).start()
        try:
            while True:
                if should_stop is not None and should_stop():
                    raise Interrupted("收到停止请求，放弃这次模型调用")
                try:
                    kind, val = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "err":
                    raise val
                if kind == "done":
                    return
                yield val
        finally:
            cancelled.set()
            if response:
                close_response(response[0])

    def chat_stream(self, messages: list[Msg], tools: list[dict],
                    should_stop=None, on_delta=None, *, max_tokens=None, request_timeout=None) -> Reply:
        """Recover this response within the optional wall-clock limit, including retries."""
        from .context_manager import prepare
        from .provider_recovery import Recovery
        from .model_observation import begin
        overrides = {} if max_tokens is None else dict(max_tokens=max_tokens)
        body = prepare(self, self._body(messages, tools, stream=True, **overrides), should_stop)
        context_attempt=0
        recovery = Recovery(self, should_stop, request_timeout=request_timeout)
        while True:
            recovery.check()
            acc = StreamAcc()
            req = self._request(body)
            observation = begin(req, self, recovery, 'sse')
            lines = self._iter_lines(req, recovery.check)
            try:
                result = stream_reply(lines, on_delta, acc, strict=True, observation=observation)
                recovery.check()
                recovery.success()
                from .context_manager import record_usage
                record_usage(self,body,result.usage)
                from .image_evidence import request_image_actions, request_image_urls
                result.input_image_actions = request_image_actions(body)
                result.input_image_urls = request_image_urls(body["messages"])
                observation.finish(reply=result)
                return result
            except Interrupted:
                observation.finish(error=Interrupted())
                recovery.emit('cancelled')
                raise
            except urllib.error.HTTPError as e:
                error=_http_failure(e)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                error = ProviderError('模型连接中断或超时', kind='network')
            except ProviderError as e:
                error = e
            except BaseException as e:
                observation.finish(error=e)
                raise
            finally:
                close = getattr(lines, 'close', None)
                if close:
                    close()
            observation.finish(error=error)
            if not acc.empty() and on_delta:
                on_delta('interrupted', '')
            revised=_recover_context(self,body,error,context_attempt,recovery.check) if acc.empty() else None
            if revised is not None:
                body=revised;context_attempt+=1;continue
            recovery.retry(error, partial=not acc.empty())


class FallbackProvider(Provider):
    """主模型不行了，换个模型接着跑。

    ★ 只在**能换**的时候换（`ProviderError.can_fallback`）：
      凭据错、请求格式错换谁都一样 —— 换过去只是把一个清楚的错误
      变成一堆不清楚的错误，而人最后看到的是最后一个模型的报错，
      真正的原因（key 过期）被埋在中间。
    ★ 流式下只在**还没出字**时换：已经开始说半句话再换，
      人会看到两段开头接在一起，而他只会得出"这个模型在胡说"。
    ★ 为什么不是"失败就重试同一个"：同一个模型限流时，退避重试是对的
      （provider 内部已经在做）；整轮失败的那种通常换一个立刻就好。

    切换会记进 `switches`，并回调 `on_switch` —— 界面上要能看见
    "这一轮换了模型"，否则人会以为一直是同一个模型在答。
    """

    name = "fallback"
    supports_stream = True

    def __init__(self, chain: list, on_switch=None, quiet: bool = False):
        if not chain:
            raise ValueError("FallbackProvider 至少要一个 provider")
        self.chain = list(chain)
        self.on_switch = on_switch
        self.switches: list[dict] = []
        self.quiet = bool(quiet)

    @property
    def profile(self) -> str:
        """这一条链的 profile（取链首）。

        ★ 为什么要有（2026-10-26）：子 agent 指定了便宜模型时，要拿"同一个 profile"
          去造 provider。没有这个属性，`provider_for` 就只能退回父的 —— 于是
          `model:` 这个配置**静默失效**，看起来配了，其实一直用主模型在跑。
        """
        return str(getattr(self.chain[0], "profile", "") or "")

    @property
    def model(self) -> str:
        return str(getattr(self.chain[0], "model", "") or "")

    @property
    def label(self) -> str:
        first = getattr(self.chain[0], "label", "?")
        return first if len(self.chain) == 1 else "%s（+%d 个备用）" % (
            first, len(self.chain) - 1)

    def _mark(self, i: int, err: Exception, nxt) -> None:
        row = {"from": getattr(self.chain[i], "label", str(i)),
               "to": getattr(nxt, "label", str(i + 1)),
               "kind": getattr(err, "kind", "?"),
               "why": str(err)[:240]}
        self.switches.append(row)
        if self.on_switch is not None:
            try:
                self.on_switch(row)
            except Exception:
                pass        # 回调出错不能影响"切过去接着跑"这件事
        if not self.quiet:
            print("⚠ 模型不可用（%s）⇒ 换 %s 接着跑：%s"
                  % (row["kind"], row["to"], row["why"][:110]))

    def chat(self, messages, tools, should_stop=None):
        last = None
        for i, p in enumerate(self.chain):
            try:
                return p.chat(messages, tools, should_stop=should_stop)
            except Interrupted:
                raise                      # 人叫停，不是错误
            except ProviderError as e:
                last = e
                if not e.can_fallback or i == len(self.chain) - 1:
                    raise
                self._mark(i, e, self.chain[i + 1])
            # ★ 认不出的异常**不换**：那说明问题不在模型侧，
            #   换过去只会把它掩盖成另一个看起来像模型错的错误。
        raise last if last is not None else RuntimeError("没有可用的模型")

    def chat_stream(self, messages, tools, should_stop=None, on_delta=None):
        last = None
        for i, p in enumerate(self.chain):
            got = False
            try:
                def delta(kind, value):
                    nonlocal got
                    got = got or kind in {'text', 'reasoning', 'tool'}
                    if on_delta:
                        on_delta(kind, value)
                return p.chat_stream(messages, tools, should_stop=should_stop, on_delta=delta)
            except Interrupted:
                raise
            except ProviderError as e:
                last = e
                # 已经吐过字 = 这一段用户已经看到了，不能从头换一个再说一遍
                if got or not e.can_fallback or i == len(self.chain) - 1:
                    raise
                self._mark(i, e, self.chain[i + 1])
        raise last if last is not None else RuntimeError("没有可用的模型")


def chain_from_env(profile: str = "HARNESS", on_switch=None,
                   quiet: bool = False) -> "Provider":
    """按配置拼一条降级链。**没配备用就原样返回主 provider**（不多包一层）。

    ★ 配置写法（凭据文件或环境变量都行）：
        HARNESS_FALLBACK_MODELS=glm-4-flash,gpt-4o-mini
      它们和主模型**共用同一份 base_url / api_key** —— 最常见的失败是
      "这个模型限流/下线了"，而换个模型比换个供应商容易配得多。

    ★ 没配的时候**不要**返回 FallbackProvider([主])：
      多包一层会让 `prov.label`、类型判断、以及"它到底支不支持流式"
      都多一个间接层，而这一层在没配置时毫无作用。
    """
    raw = ""
    import os as _os
    pre = (profile or "HARNESS").rstrip("_") + "_"
    raw = _os.environ.get(pre + "FALLBACK_MODELS", "")
    if not raw:
        try:
            raw = OpenAICompatProvider._read_creds().get(pre + "FALLBACK_MODELS", "")
        except Exception:
            raw = ""
    models = [m.strip() for m in str(raw).split(",") if m.strip()]
    main = OpenAICompatProvider(profile=profile)
    if not models:
        return main
    chain = [main]
    for m in models:
        if m == main.model:
            continue                    # 和主模型一样就没必要列
        chain.append(OpenAICompatProvider(profile=profile, model=m))
    if len(chain) == 1:
        return main
    return FallbackProvider(chain, on_switch=on_switch, quiet=quiet)
