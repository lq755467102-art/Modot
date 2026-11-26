"""Same-request transport recovery. No task scheduling and no tool replay."""
# 主要引用方包括 `provider.py`。
import socket
import threading
import time
import uuid


def close_response(response):
    if response is None:
        return
    try:
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
    # close() can wait for a reader lock; never block cancellation on that lock.
    threading.Thread(target=lambda: _close(response), daemon=True).start()


def _close(response):
    try:
        response.close()
    except (OSError, ValueError):
        pass


def public_event(row):
    """Keep the historical HTTP code without overloading the event status."""
    if row.get('act') != 'provider_recovery' or type(row.get('status')) is not int:
        return row
    public = dict(row)
    public.setdefault('httpStatus', public.pop('status'))
    return public


# 模型提供方的一次请求恢复状态。
# `id` 和 `attempt` 关联同一请求的尝试，`deadline` 限定整个重连窗口，`stop` 取自运行方的取消信号。
class Recovery:
    def __init__(self, provider, stop, *, request_timeout=None):
        self.provider, self.stop = provider, stop
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.request_deadline = self.started + request_timeout if request_timeout is not None else None
        self.deadline = None
        self.attempt = 0

    def emit(self, state, **fields):
        from .context_inspection import active_request
        owner = active_request.get()
        if owner:
            session, role = owner
            session.emit('provider_recovery', role=role, state=state, requestId=self.id,
                         attempt=self.attempt + 1, elapsedMs=round((time.monotonic()-self.started)*1000), **fields)

    def check(self):
        from .provider import Interrupted, ProviderError
        if self.stop and self.stop():
            self.emit('cancelled')
            raise Interrupted('收到停止请求，取消模型重连')
        now = time.monotonic()
        if self.request_deadline is not None and now >= self.request_deadline:
            raise ProviderError('模型请求达到调用时限', kind='request_deadline', can_fallback=False)
        if self.deadline is not None and now >= self.deadline:
            self.emit('exhausted', errorKind='recovery_deadline')
            raise ProviderError('上游暂不可用，恢复时限已到；进度已保留', kind='recovery_exhausted', can_fallback=False)
        return False

    def retry(self, error, partial=False):
        from .provider import ProviderError, backoff
        # 只重试可恢复的传输/响应问题；鉴权或协议等错误直接交回调用方，不扩大为任务重跑。
        if error.kind not in {'network', 'server', 'rate_limit', 'empty_response', 'incomplete_stream'}:
            self.emit('failed', errorKind=error.kind, httpStatus=error.status)
            raise error
        # 恢复时限从第一次可重试失败开始共享，后续重试不能重新获得一整段时限。
        if self.deadline is None:
            self.deadline = time.monotonic() + getattr(self.provider, 'recovery_seconds', 60)
        if self.attempt >= max(0, self.provider.retries) + (error.kind == 'network'):
            self.emit('exhausted', errorKind=error.kind, httpStatus=error.status, partial=partial)
            raise ProviderError('上游暂不可用，自动重连未成功；进度已保留（%s）' % error.kind,
                                kind='recovery_exhausted', can_fallback=False) from error
        delay = min(max(backoff(self.attempt), getattr(error, 'retry_after', 0)),
                    max(0, self.deadline-time.monotonic()))
        self.emit('waiting', errorKind=error.kind, httpStatus=error.status,
                  retryAfterSeconds=round(delay, 2), partial=partial)
        end = time.monotonic() + delay
        # 退避也持续响应停止和总恢复期限；等完退避不意味着一定还能发下一次请求。
        while time.monotonic() < end:
            self.check()
            time.sleep(min(.05, max(0, end-time.monotonic())))
        self.check()
        self.attempt += 1

    def success(self):
        if self.attempt:
            self.emit('recovered')
