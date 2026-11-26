"""Versioned contract for the hardened control plane; additive to legacy APIs."""
# 主要引用方包括 `workbench.py` 和 `bin/server.py`。

PROTOCOL_VERSION = 1
OPERATION_STATES = ['processing', 'queued', 'completed', 'failed', 'unknown']
RUN_STATES = ['running', 'stopping', 'idle', 'unknown', 'completed', 'failed', 'interrupted', 'waiting_approval']
FEATURES = ['request-receipts', 'durable-queue', 'process-recovery', 'effective-policy', 'event-cursor', 'event-pagination', 'session-runtime']

SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'HarnessControlProtocol',
    'type': 'object',
    '$defs': {
        'EventsPage': {'type':'object','required':['events','has_more','next_after'], 'properties': {
            'events': {'type':'array'}, 'has_more': {'type':'boolean'},
            'next_after': {'type':'integer','minimum':0}}},
        'Action': {'type':'object','required':['run_id','action_id','state'], 'properties': {
            'run_id': {'type':'string'}, 'action_id': {'type':'string'},
            'state': {'enum':['running','completed','failed','waiting_approval','cancelled_before_start']}}},
        'Operation': {'type':'object', 'required':['id','state','path'], 'properties': {
            'id': {'type':'string'}, 'state': {'enum':OPERATION_STATES},
            'path': {'type':'string'}, 'code': {'type':['integer','null']},
            'response': {'type':['object','null']}}},
        'Event': {'type':'object', 'required':['seq','act'], 'properties': {
            'seq': {'type':'integer','minimum':1}, 'act': {'type':'string'}}},
        'Runtime': {'type':'object', 'required':['state','active'], 'properties': {
            'state': {'enum':RUN_STATES}, 'active': {'type':'boolean'},
            'run_id': {'type':'string'}, 'policy': {'type':['string','null']}}},
        'Command': {'type':'object', 'properties': {
            'text': {'type':'string'}, 'task': {'type':'string'},
            'crew': {'enum':['l0']},
            'images': {'type':['array','null']},
            **{name: {'type':['integer','null'],'minimum':0} for name in ('ctx','max_turns','min_chars','min_reads')}}},
        'Error': {'type':'object','required':['error'], 'properties': {
            'error': {'type':'string'}, 'request_id': {'type':'string'}}},
    }
}

# 运行命令可省略配置字段；显式提供时校验范围。
# 布尔值虽然是 Python 整数子类型，这里按无效预算处理。
def validate_command(body):
    for name in ('text','task','crew'):
        if name in body and not isinstance(body[name], str):
            raise ValueError('%s must be a string' % name)
    if 'crew' in body and body['crew'] != 'l0':
        raise ValueError('旧执行档位已退役，请使用 L0')
    if 'images' in body and body['images'] is not None and not isinstance(body['images'], list):
        raise ValueError('images must be an array')
    for name in ('ctx','max_turns','min_chars','min_reads'):
        if name in body and body[name] is not None:
            if isinstance(body[name], bool) or not isinstance(body[name], int) or body[name] < 0:
                raise ValueError('%s must be a nonnegative integer' % name)
