"""Data-only native worker contributions. Draft wire contract, no code loader."""
# 主要引用方包括 `worker_plugins.py` 和 `plugin_packages.py`。
from copy import deepcopy
import json
import re
from . import workbench as W

PROTOCOL = 'modot.worker/1-draft'
CAPABILITIES = frozenset({'native-l0', 'independent-history', 'messages', 'resume',
    'cancel', 'structured-result', 'read-code', 'isolated-write', 'isolated-check',
    'cross-parent-run', 'automatic-wakeup'})


def encoded(value, limit=32768):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        raise W.Problem('worker_data', '委派数据须为有限 JSON 值') from None
    if len(text.encode('utf8')) > limit:
        raise W.Problem('worker_capacity', '委派数据超过本接口容量，原记录保留', 413)
    return text


def schema(spec, depth=0):
    """Publish exactly the supported JSON Schema subset; never ignore keywords."""
    allowed = {'type','properties','required','additionalProperties','items','enum',
               'description','minLength','maxLength','minimum','maximum','minItems','maxItems'}
    if (not isinstance(spec, dict) or depth > 8 or set(spec)-allowed
            or spec.get('type') not in {'object','array','string','integer','number','boolean','null'}):
        raise W.Problem('worker_schema', '不支持的 Schema 类型、关键字或嵌套深度')
    kind = spec['type']
    typed={'object':{'properties','required','additionalProperties'},'array':{'items','minItems','maxItems'},
           'string':{'minLength','maxLength'},'integer':{'minimum','maximum'},'number':{'minimum','maximum'},
           'boolean':set(),'null':set()}
    if set(spec)-{'type','enum','description'}-typed[kind]:
        raise W.Problem('worker_schema','Schema 关键字不适用于所声明类型')
    if 'description' in spec and not isinstance(spec['description'],str):
        raise W.Problem('worker_schema','Schema description 须为文字')
    for key in ('minLength','maxLength','minItems','maxItems'):
        if key in spec and (type(spec[key]) is not int or spec[key] < 0):
            raise W.Problem('worker_schema', 'Schema 长度约束无效')
    for key in ('minimum','maximum'):
        if key in spec and type(spec[key]) not in (int, float):
            raise W.Problem('worker_schema', 'Schema 数值约束无效')
    if kind == 'object':
        props = spec.get('properties', {})
        required = spec.get('required', [])
        if (not isinstance(props,dict) or len(props)>64 or not isinstance(required,list)
                or any(not isinstance(k,str) or k not in props for k in required)
                or type(spec.get('additionalProperties',False)) is not bool):
            raise W.Problem('worker_schema', 'Schema 对象字段无效')
        for value in props.values(): schema(value,depth+1)
    elif kind == 'array': schema(spec.get('items'),depth+1)
    if 'enum' in spec and (not isinstance(spec['enum'],list) or not spec['enum']):
        raise W.Problem('worker_schema', 'Schema enum 须为非空列表')
    encoded(spec,16384)
    return deepcopy(spec)


def validate(value, spec, path='$'):
    kind = spec['type']
    types = {'object':dict,'array':list,'string':str,'integer':int,'boolean':bool,'null':type(None)}
    valid = type(value) in (int,float) if kind == 'number' else type(value) is types[kind]
    if not valid or 'enum' in spec and encoded(value) not in [encoded(v) for v in spec['enum']]:
        raise W.Problem('worker_data', path+' 不符合声明类型或枚举')
    if kind == 'object':
        props = spec.get('properties',{})
        if set(spec.get('required',[]))-set(value) or not spec.get('additionalProperties',False) and set(value)-set(props):
            raise W.Problem('worker_data', path+' 缺少字段或包含未声明字段')
        for key in set(value)&set(props): validate(value[key],props[key],path+'.'+key)
    if kind == 'array':
        for index,item in enumerate(value): validate(item,spec['items'],f'{path}[{index}]')
    for key, actual, compare in [('minLength',len(value) if kind=='string' else None,lambda a,b:a>=b),
            ('maxLength',len(value) if kind=='string' else None,lambda a,b:a<=b),
            ('minItems',len(value) if kind=='array' else None,lambda a,b:a>=b),
            ('maxItems',len(value) if kind=='array' else None,lambda a,b:a<=b),
            ('minimum',value if kind in {'number','integer'} else None,lambda a,b:a>=b),
            ('maximum',value if kind in {'number','integer'} else None,lambda a,b:a<=b)]:
        if key in spec and (actual is None or not compare(actual,spec[key])):
            raise W.Problem('worker_data', path+' 不符合 '+key)
    encoded(value)


def declarations(value):
    if not isinstance(value,list) or len(value)>8:
        raise W.Problem('worker_manifest','workers 须为最多8项的数组')
    names=set()
    for item in value:
        required={'name','protocol','description','method','inputSchema','outputSchema','requires'}
        if not isinstance(item,dict) or set(item)-required-{'optional','modelRole'} or required-set(item):
            raise W.Problem('worker_manifest','委派贡献字段缺失或未知')
        name=item['name']
        if not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9_-]{0,47}',name) or name in names:
            raise W.Problem('worker_manifest','委派贡献名称无效或重复')
        names.add(name)
        if 'modelRole' in item:
            from .task_core_models import ROLES
            if not isinstance(item['modelRole'], str) or item['modelRole'] not in ROLES:
                raise W.Problem('worker_model_role', '任务核心声明的模型角色无效')
        if item['protocol']!=PROTOCOL:
            raise W.Problem('worker_protocol','尚不支持此委派协议版本',409)
        for key,limit in [('description',240),('method',2000)]:
            if not isinstance(item[key],str) or not 1<=len(item[key])<=limit:
                raise W.Problem('worker_manifest',key+' 长度无效')
        for key in ('requires','optional'):
            caps=item.get(key,[])
            if not isinstance(caps,list) or any(not isinstance(c,str) for c in caps) or len(caps)>32 or len(set(caps))!=len(caps):
                raise W.Problem('worker_manifest','能力列表无效')
        missing=set(item['requires'])-CAPABILITIES
        if missing: raise W.Problem('worker_capability','缺少必需能力：'+', '.join(sorted(missing)),409)
        schema(item['inputSchema']);schema(item['outputSchema'])
        if item['inputSchema']['type']!='object':
            raise W.Problem('worker_schema','委派 inputSchema 根类型须为 object')
    encoded(value,128*1024)
    return deepcopy(value)
