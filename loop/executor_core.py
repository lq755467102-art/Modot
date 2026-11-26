"""Native executor work records. No role coordinator, queues or worker lifecycle.

The session owns this state; code tools continue to own versions and receipts.
Recording work is optional and never schedules another model or grants access.
"""
# 主要引用方包括 `native_entry.py`。
from copy import deepcopy
import hashlib
import json

from . import code_service as C, evidence, workbench as W
from .tools import Tool
from .working_context import append_state_update, install as install_summary


class ExecutorCore:
    def __init__(self, session, root, *, stop=lambda: False):
        self.session, self.root, self.stop = session, root, stop
        self.vision_available = False
        self.worker_options = []
        self.task = deepcopy(getattr(session, 'task_input', {}))
        from .github_workroom import CurrentPullRequest

        self.github = CurrentPullRequest(session)
        saved = session.load()
        self._persisted_runtime_facts = saved.get('runtimeFacts')
        # 进度只在同一逻辑任务内续用；开始新任务时不能继承上一任务的缺陷和检查作为当前事实。
        old = saved.get('executorWork', {})
        self.state = old if old.get('taskId') == self.task.get('id') else dict(
            taskId=self.task.get('id'), issues={}, checks={}, versions={})
        self.state.update(runId=session.run_id, demandRevision=self.task.get('revision'))
        session.executor_core = self
        self.save()

    def save(self):
        self.session.save(executorWork=self.state)

    def install(self, registry):
        from .memory_providers import acquire
        lease = next((getattr(registry.get(name), 'memory_provider', None) for name in registry.names()
                      if getattr(registry.get(name), 'memory_provider', None)), None)
        self.session.memory_provider = lease or acquire(self.session.dir.name, session=self.session, model=True)
        self.session.memory_provider.session = self.session
        from .context_registry import request_scope
        registry.scope = request_scope(self.session, 'executor')
        # Management stays in the existing application UI. Keep engineering,
        # subtask, recovery and evidence tools; freeze optional plugin tools once.
        registry.omit({'propose_delegation', 'record_delegation_progress',
            'list_plugin_catalog', 'list_installed_plugins', 'install_catalog_plugin',
            'preview_local_plugin', 'install_local_plugin', 'enable_plugin_components'})
        from . import plugin_mcp, plugins
        try:
            enabled = plugins.grants(self.session.dir.name)
        except (OSError, ValueError, W.Problem):
            enabled = []  # Unavailable grants cannot expose optional plugin capabilities.
        if not any(p.get('skillsEnabled') for p in enabled):
            registry.omit({'read_plugin_skill', 'read_plugin_file'})
        if not any(p.get('remoteEnabled') for p in enabled):
            registry.omit({'inspect_plugin_tools', 'call_plugin_tool'})
        if not any(s.get('supported') and s.get('url') == plugin_mcp.GITHUB_RESOURCE
                   for p in enabled if p.get('remoteEnabled') for s in p.get('servers', [])):
            registry.omit({'find_github_repositories'})
        if any(p.get('contextEnabled') for p in enabled):
            from .context_plugins import install
            self.context_plugins = install(self.session, registry, enabled)
        self.budget_plugin_reads = {}
        for package in enabled:
            if not package.get('contextEnabled'):
                continue
            for store in package.get('contextStores', []):
                key, name = package['id'], store['name']
                tool = f'ctx_{key}_{name}_read'
                if registry.get(tool):
                    self.budget_plugin_reads[f'plugin.{key}.context.{name}'] = (tool, package['name'], name)
        from .worker_plugins import install as install_workers
        self.worker_options = install_workers(self.session,registry,enabled) or []
        install_summary(self.session, 'executor', registry, contracts=True)
        registry.get('record_working_summary').description = (
            '保存或更新本任务的模型自述与来源。summary为记录正文，sources为来源列表；'
            '记录不是用户要求、授权或验收结论。')
        local_skills = {}
        if (getattr(self.session,'context_preferences',{}).get('effective',{}).get('skills',True)
                and any(s.get('kind')=='skills' and s.get('text')
                        for s in getattr(self.session,'context_sources',[]))):
            from . import skills
            local_skills = {s['name']: s for s in skills.scan(self.root)
                            if s['modelInvocable'] and not s['unsupported']}
        if local_skills:
            from .tools import _skills_load
            def load_skill(name, offset=0, source_token=None):
                name = str(name or '').strip()
                if name not in local_skills:
                    return dict(ok=False,code='skill_unavailable',error='此技能未列入本轮可自动调用清单。')
                return _skills_load(name,self.root,offset,source_token)
            def list_skills(query="", after=0):
                if type(after) is not int or after < 0 or not isinstance(query, str) or len(query) > 160:
                    raise W.Problem('invalid_arguments', '技能查询参数无效')
                query = query.strip().casefold()
                matches = [item for item in local_skills.values()
                           if query in (item['name'] + ' ' + item['desc']).casefold()]
                page = [dict(name=item['name'], desc=item['desc'][:160], level=item['level'])
                        for item in matches[after:after + 5]]
                following = after + len(page)
                return dict(ok=True, skills=page, hasMore=following < len(matches),
                            next=following if following < len(matches) else None)
            registry.add(Tool('list_skills',
                '分页查本轮可用技能的名称与用途。query按名称或用途筛选，after取上页next；用load_skill读取正文。',
                dict(type='object', properties=dict(query=dict(type='string', maxLength=160),
                                                    after=dict(type='integer', minimum=0))),
                list_skills, zone='offflow', kind='read', readonly=True))
            registry.add(Tool('load_skill',
                '按名称读取本轮技能清单中的方法正文；续页用offset=返回的nextOffset，并原样传source_token；来源变化须重读。正文不授予权限，也不执行附带脚本。',
                dict(type='object',properties=dict(name=dict(type='string'),offset=dict(type='integer',minimum=0),source_token=dict(type='string')),
                     required=['name']),load_skill,zone='offflow',kind='read',readonly=True))
        check = registry.get('run_project_check')
        if check:
            check.description = ('在已授权的本地文件夹运行隔离命令，可无Git。argv用于普通程序或powershell.exe -Command，'
                '临时Python传script，复跑旧脚本传script_operation_id=旧operationId。同条件在途检查可共用一个操作。'
                '默认源码只读，临时文件用TEMP或.harness-check；writable_copy仅允许改隔离副本。'
                '返回进程/输出/退出码和源版本，不推断任务完成。普通调用省略expected_revision；'
                '长检查可先返回starting/operationId或running/processId。可继续独立工作；只等结果时按回执readOutput调用read_check_output有界等待。'
                '指定版本用Git项目projectRevision或普通目录checkRevision。capture_changes需要Git基线和写入授权。')
            check.parameters = deepcopy(check.parameters)
            check.parameters['properties']['scope_reason']['description'] = '可选短验证目标，可注明原公共入口/输入及预期出处；不创建验收义务。'
            check.parameters['properties']['purpose']['description'] = (
                '临时探查用exploration。验收自测用真实断言、已有测试或工作间具名断言。'
                '只打印PASS/FAIL只能用于诊断。acceptance记录验证意图，回执只支持实际版本和范围，'
                '不会自动新增用户必验项。真实业务失败须报告，不能用改用途消除。')
            check.parameters['properties']['environment_id'] = dict(type='string',description=
                '可选：省略即复用同任务运行时。仅传返回的environment_id（process.environmentId）或new；'
                'environmentFingerprint/旧environment是指纹，不能当ID。new创建独立保留目录，文件写MODOT_TEST_ENV。',
                pattern='^(new|[0-9a-f]{32})$')
        from . import project_image_view, projects
        registry.add(Tool(
            "read_image",
            "直接查看已授权项目目录内的原图，例如截图、示意图或有文字的图片。"
            "把 PNG、JPEG、WebP、静态 GIF 原图送入模型上下文。给 view 可裁切或旋转；"
            "3D 打印 G-code 用 view={} 预览 xy 和打印对象，也可指定其他平面、对象、层或旋转。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "description": "文件在项目资料目录内的相对路径"},
                    "view": project_image_view.SCHEMA,
                    "resource_id": {"type": "string", "description": "目录资料 ID，只有一个授权目录时可省略"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda path, resource_id=None, view=None: projects.tool_read_image(self.root, path, resource_id, view=view),
            zone="offflow", readonly=True, kind="read",
        ))
        from . import vision_batch, vision_transcription
        batch = vision_batch.install(self.session, self.root, registry, self.stop)
        if batch:
            registry.omit({'read_image'})
        self.vision_available = not batch and vision_transcription.install(self.session, self.root, registry, self.stop)
        check_environments = C.adapter(self.root, 'check_environments')
        from . import project_shell
        string = dict(type='string')
        registry.add(Tool('run_project_shell', '运行项目终端命令。查看仓库用 inspect_git_repository；克隆用 workspace 组的导入工具，以便工作间定位代码。请求批准和帮我批准都在项目内隔离执行，变更同步时保留外部编辑。访问项目外或网络时用 outside_reason 说明本条命令的用途，由所选模式审批。完全访问权限使用本机终端。SSH 身份通过聊天中的本机身份入口办理，只返回公钥。',
            dict(type='object', properties=dict(resource_id=dict(type='string'),
                 argv=dict(type='array',items=dict(type='string')),timeout=dict(type='integer',minimum=1,maximum=120),
                 outside_reason=dict(type='string')),required=['resource_id','argv'],additionalProperties=False),
            lambda **args: project_shell.run(self.root, **args), zone='offflow', kind='exec'))
        registry.add(Tool("inspect_git_repository",
            "用内置 Git 查看已授权项目的状态、提交、分支或差异；仓库在子目录时传 directory。log 看近期记录，show 看一次提交的说明及改动文件（默认 HEAD）。diff/staged 看变更概览，传 file 看文件补丁。",
            dict(type="object", properties=dict(resource_id=string, directory=string, file=string, commit=string,
                operation=dict(type="string", enum=["status","branches","log","show","diff","staged"])),
                required=["resource_id"], additionalProperties=False),
            lambda **args: project_shell.inspect_git(self.root, **args), zone="offflow", kind="read", readonly=True))
        def environment(resource_id, operation='list', environment_id=None):
            if operation not in {'list', 'release'}:
                raise W.Problem('invalid_arguments', '使用 list 查看，或 release 加 environment_id 释放')
            return check_environments(resource_id, environment_id=environment_id, remove=operation == 'release')
        registry.add(Tool('review_test_environment','查看或显式释放本任务已有准备环境；是否复用、新建由你判断，历史通过不代表当前通过。',
            dict(type='object',properties=dict(resource_id=dict(type='string'),operation=dict(type='string',enum=['list','release']),
                 environment_id=dict(type='string')),required=['resource_id']),environment,zone='offflow',kind='meta'))
        def scenario(resource_id, operation='list', scenario_id=None, version=None, definition=None, expected_version=None):
            C.context_resource(self.root,resource_id,'execute')
            from . import test_workroom
            return test_workroom.manage(self.session.dir.name,self.task.get('id'),operation,
                scenario_id,version,definition,expected_version)
        registry.add(Tool('test_workroom','按需列出/读取/保存测试场景。内置模板只读，另存到当前任务；更新带expected_version。运行仍用run_project_check的scenario引用，环境复用沿用environment_id。',
            dict(type='object',properties=dict(resource_id=dict(type='string'),operation=dict(type='string',enum=['list','read','save']),
                scenario_id=dict(type='string'),version=dict(type='string'),expected_version=dict(type='string'),
                definition=dict(type='object',description='name/description/runner(python或command)/script或argv/files/defaults/requires；script/files传源码原文，无需repr或额外JSON编码。不自动安装工具。')),
                required=['resource_id'],additionalProperties=False),scenario,zone='offflow',kind='meta'))
        output = registry.get('read_check_output')
        if output:
            output.parameters = deepcopy(output.parameters)
            output.description += '只等终态时传进程或操作ID和wait_ms；等待新输出时另传上次next作为after。'
            output.parameters['properties']['stream'] = dict(type='string',enum=['stdout','stderr'],
                description='可选：分流日志，配合after；省略读取合并日志。旧记录可能只有合并日志。')
        registry.add(Tool('read_task_requirements', '按需读取本任务用户原话、补充和答复；工作记录不能撤销用户要求。',
            dict(type='object', properties=dict(after=dict(type='integer'))), self.requirements,
            zone='offflow', kind='read', readonly=True))
        registry.add(Tool('read_check_queue', '读取自己的排障尝试及检查回执。没有助手队列；历史通过只证明对应版本。',
            dict(type='object', properties={}, additionalProperties=False), self.read, zone='offflow', kind='read', readonly=True))
        registry.add(Tool('replace_failed_check_entry',
            '可选证据关联：仅同版本、同环境、同用途和同测试范围的入口更正。自定义Python只支持补缺失导入且原断言与主体不变。保留原失败；不能替代业务断言失败、改预期或缩小覆盖。',
            dict(type='object',properties={key:string for key in ('failed_action_id','passed_action_id','reason')},
                 required=['failed_action_id','passed_action_id','reason']),self.replace_entry,zone='offflow',kind='meta'))
        registry.add(Tool('record_troubleshooting', '按需记录阻塞、假设和实际结果；相同问题用原 issue_id 更新。'
            'resolved 是解决声明，须同版本成功回执支持；不会派发助手或强制继续循环。',
            dict(type='object', properties=dict(resource_id=string, signature=string, hypothesis=string,
                result=string, state=dict(type='string', enum=['investigating', 'blocked', 'resolved']),
                issue_id=string, evidence_action_id=string),
                required=['resource_id', 'signature', 'hypothesis', 'result', 'state']),
            self.troubleshoot, zone='offflow', kind='meta'))
        from . import repository_import
        thread = W.registry()["threads"].get(self.session.dir.name, {})
        if thread and not any(thread.get(key) for key in ("readonlyEvidence", "subtaskOf", "archived", "scheduledRun")):
            registry.add(Tool("import_public_repository",
                "用专用 AppContainer 匿名下载公开 HTTPS 仓库的默认分支。"
                "项目会话默认导入当前项目下的新子目录；独立会话使用已选工作总目录创建项目。"
                "需用户批准地址与新目录，可选 name 指定目录名。回执含 resourceId、提交及项目内相对路径（如有）。",
                dict(type="object", properties=dict(url=string, name=string),
                     required=["url"], additionalProperties=False),
                repository_import.tool, zone="offflow", kind="lifecycle"))
            registry.add(Tool("import_private_repository",
                "经用户批准，用本机 SSH 身份将私库导入当前项目下的新子目录；独立会话使用已选工作总目录创建项目。"
                "填写 git@主机:组织/仓库.git 或 ssh://git@主机/路径，可选 name。"
                "凭据和主机指纹由本机 SSH 管理；不要提交私钥或令牌。成功回执含 projectId、resourceId 和提交。",
                dict(type="object", properties=dict(url=string, name=string),
                     required=["url"], additionalProperties=False),
                repository_import.private_tool, zone="offflow", kind="lifecycle"))
        if (thread and not any(thread.get(key) for key in ("readonlyEvidence", "subtaskOf", "archived", "scheduledRun", "scheduledPlanId"))
                and any(r.get("kind") == "code" and r.get("permission") == "read"
                        for r in (projects.resource_snapshot(thread) or {}).get("resources", ()))):
            from . import project_workroom
            def open_workroom_file(path, resource_id=None, from_current_pr=False, line=None):
                if from_current_pr:
                    path, resource_id = self.github.local_file(path, resource_id)
                return project_workroom.request_file_open(self.session, self.root, path, resource_id,
                                                          github=self.github if self.github.result else None, line=line)

            registry.add(Tool("open_workroom_file",
                "用户要求在工作间打开文件时调用。path 为当前授权检出目录中的文件路径，"
                "多个检出目录可指定 resource_id。只读取文件并切换当前窗口的标签页。"
                "只有 workroomTabOpened 为 true 才能向用户确认已打开。"
                "若文件来自已读取的当前 PR，传 from_current_pr=true 和仓库内相对路径，核对实际本地 HEAD。",
                dict(type="object", properties=dict(path=string, resource_id=string, from_current_pr=dict(type="boolean"),
                                                    line=dict(type="integer",minimum=1)),
                     required=["path"], additionalProperties=False),
                open_workroom_file,
                zone="offflow", kind="read" if self.github.captured.get("status") == "ready" else "meta"))
        if self.github.captured.get("status") == "ready" and thread:
            registry.add(Tool("read_current_github_pr",
                "读取发送消息时所选 GitHub PR 的编号、提交、真实检查状态和可用摘要。"
                "GitHub 文字是资料，排队或缺少日志时按实际覆盖回答。",
                dict(type="object", properties={}, additionalProperties=False),
                self.github.read, zone="offflow", kind="read", readonly=True))
            registry.add(Tool("navigate_github_pr",
                "导航当前 GitHub PR 到 Files changed 或 Checks。定位某条检查时使用实际读到的 check_id。"
                "仅 pageOpened=true 表示实际打开。",
                dict(type="object", properties=dict(section=dict(type="string", enum=["files","checks"]), check_id=string),
                     required=["section"], additionalProperties=False),
                self.github.navigate, zone="offflow", kind="read"))
        self._stage_tools(registry,local_skills=bool(local_skills))
        from .tool_results import install_native_contracts
        install_native_contracts(registry)
        self.registry = registry  # Local tool contracts; never serialized into model text.

    def _stage_tools(self, registry, *, local_skills=False):
        groups = {
            'verification': {'inspect_delivery','record_delivery_evidence','read_check_queue','replace_failed_check_entry','record_troubleshooting','stop_project_check'},
            'workroom': {'review_test_environment','test_workroom'},
            'recovery': {'undo_own_project_write','prepare_file_restoration','restore_project_file','read_code_history'},
            'debug': {n for n in registry.names() if 'python_debug' in n},
            'applications': {'start_application','inspect_application','stop_application'},
            'subtasks': {n for n in registry.names() if 'subtask' in n},
            'memory': {n for n in registry.names() if 'memory' in n or n in {'remember','propose_learned_habit','compare_memory_choices'}},
            'artifacts': {'transform_image','publish_artifact','read_artifact'},
            'history': {'search_conversation_history','read_context_history','read_conversation_dialogue','read_action_evidence','read_correction_evidence','read_project_activity','read_discussion_background','list_project_conversations','read_shared_conversation'},
            'workspace': {'initialize_code_project','prepare_code_branch','import_public_repository','import_private_repository'},
            'plugins': {n for n in registry.names()
                        if 'plugin' in n or n.startswith('ctx_') or n == 'find_github_repositories'},
        }
        advertised = set(registry.names())
        core = advertised & {
            'ask_human','read_project_resource','read_image','transcribe_image','inspect_code','search_project_code',
            'read_check_output','edit_project_file','write_project_file','run_project_check','run_project_shell',
            'inspect_git_repository','open_workroom_file','read_current_github_pr','navigate_github_pr',
            'record_working_summary','read_task_requirements',
        }
        if local_skills:
            core.add('load_skill')
            groups['skills'] = {'list_skills'}
        if getattr(self.session, 'task_history_start', 0) and 'search_conversation_history' in advertised:
            core.add('search_conversation_history')
        core |= advertised & {'inspect_worker_plugins', 'delegate_plugin_worker',
                              'manage_plugin_worker', 'submit_worker_result', 'transcribe_image_batch'}
        selected = set(self.state.get('toolGroups', [])) & set(groups)
        # Explicit acceptance obligations must remain discoverable on resume.
        from . import delivery_review as D
        path, _ = D.storage(self.session.dir.name)
        verification_required = any(
            r.get('required') and r.get('logicalTaskId') in {None, self.task.get('id')}
            for r in D.load(path)['requirements']
        )
        if verification_required:
            selected.add('verification')
        self.budget_groups = groups
        self.budget_base_tools = core | {'use_tool_group'}
        self.budget_required_tools = self.budget_base_tools | (groups['verification'] if verification_required else set())
        self.budget_requested_groups = set()

        check = registry.get('run_project_check')
        check_parameters = deepcopy(check.parameters) if check else None

        def refresh():
            if check:
                check.parameters = deepcopy(check_parameters)
                if 'workroom' not in selected:
                    for field in ('scenario', 'parameters', 'environment_id'):
                        check.parameters['properties'].pop(field, None)
            registry.select(core | {'use_tool_group'} | set().union(*(groups[g] for g in selected)))

        def choose(group):
            if group not in groups:
                raise W.Problem('invalid_tool_group', '选择已列出的工具组')
            selected.add(group)
            self.budget_requested_groups.add(group)
            self.state['toolGroups'] = sorted(selected)
            self.save()
            refresh()
            if group == 'memory' and (not self.session.memory_provider.available or
                    not getattr(self.session, 'memory_provider_ready', True)):
                return dict(ok=True, group=group, tools=[], status='unavailable',
                    notice='记忆实现未启用或已撤销；普通任务可以继续。启用新的实现从下一轮生效。')
            names = sorted(groups[group].intersection(registry.names()))
            if group == 'plugins' and 'call_plugin_tool' not in names:
                return dict(ok=True, group=group, tools=names, remoteTools='unavailable',
                    notice='本轮没有已授权的远端 MCP 工具。需要外部服务时，可由用户在插件页连接并在下一轮使用。')
            return dict(ok=True, group=group, tools=names,
                notice='定义将在下次请求提供；实际调用仍核对当前授权。')

        worker_hint = '/可委派任务核心与子 Agent' if 'inspect_worker_plugins' in advertised else ''
        skill_hint = 'skills=查询本轮可用技能；' if local_skills else ''
        registry.add(Tool('use_tool_group',
            '先确定要做的动作，再按需取得工具定义。verification=检查记录/必验登记；'
            'workroom=重复测试场景/准备材料，普通命令与一次性脚本无需启用；recovery=撤销与恢复；'
            'debug=Python调试；applications=运行应用；subtasks=子任务；memory=记忆采集/升格；'
            f'{skill_hint}artifacts=成果/图片；history=本会话记录、分支起点、同项目会话标题/运行状态/公开问答（按需）。用户可在会话菜单选择“从此会话分支”；workspace=项目/分支/导入公开或 SSH 私库；plugins=已授权插件清单/服务/调用{worker_hint}。不会扩大权限。',
            dict(type='object', properties=dict(group=dict(type='string', enum=sorted(groups))), required=['group']),
            choose, zone='offflow', kind='read', readonly=True))
        refresh()

    def instruction(self):
        from . import delivery_review as D
        path, _ = D.storage(self.session.dir.name)
        rows = D.load(path)['requirements']
        required = any(row.get('required') and row.get('logicalTaskId') in {None, self.task.get('id')} for row in rows)
        text = ('\n从用户原话、原文档/调用或fixture取验证输入，串到实际输出；别名与输出标识分开。'
                '查看项目原图用 read_image，传相对路径；生成投影或裁切时加 view。附件和目录列表用 read_project_resource。'
                '用户要求把读到的文字、数值或机器格式写入指定文件时，文件只写目标内容；来源、解释和不确定项放在答复或回执中。'
                '沿既有兼容分支扩展，新要求优先；自建预期冲突时先核对依据，不用新实现反过来定义需求。'
                '返回约定不明先读定义/调用示例，必要时短探查确认，勿连续猜。'
                '检查失败先定位；探针/环境受阻可说明未验证范围并结束路线，不必逐份修绿。'
                 '验证修复时，从用户要求或现有契约取输入和预期，修前复现后用同一用例及输入验证修后。'
                 '单独核对命令退出与断言报告；缺少目标断言结果时按实际覆盖总结。'
                 '检查回执表示当时的状态。按同一 operationId 的较新回执确认当前状态。'
                '编辑填文件revision；普通run_project_check省略expected_revision，指定版本取目录的checkRevision或项目projectRevision。'
                '临时Python用run_project_check(script=正文)，反复使用可保存到可写临时区。'
                '探查用purpose=exploration；真实失败须报告，打印或退出0不证明验收。'
                '日志用原process_id和after/query定点读取，不为读日志重跑；环境未变可复用目录。'
                '\n用图说明流程或分支时，优先在答复正文写 Markdown 的 ```mermaid 围栏，首行使用 flowchart TD，'
                '节点文字用双引号括住。用户明确要求 PNG 等图片文件时，按要求生成图片成果。'
                '普通任务直接引用自动回执，不必创建要求或手工登记覆盖。最终答复可结束运行，结束不表示验收通过。')
        if self.vision_available:
            text += ('\n需要精确转写图片中的文字（区分大小写、标点和顺序）时，优先调用 transcribe_image。'
                     '如果源文件需先渲染，就先生成清晰图片再转写。根据工具返回的正文完成用户要求的交付。')
        if self.worker_options:
            text += ('\n任务核心用用户选定的另一模型补足父核心不擅长的专项能力。'
                     '子 Agent 沿用父核心的主模型配置，代劳父核心能做的耗时工作。'
                     '用 inspect_worker_plugins 查找 available 的贡献，核对契约和必要资料后用 delegate_plugin_worker 委派。'
                     '需要选投影并精确转写或多视图复核时，尽早把已授权的源文件或首张可读投影委派给可用的视觉核心。收到候选后，父核心验收并交付。'
                     '只差委派成果时用 manage_plugin_worker 的 query 和 wait_ms=30000 等待，取得 result_id 后读取成果。'
                     '逐字文件交付默认把已读候选回执的 writeParts 传给 write_project_file 的 parts。'
                     '用候选正文当底稿；partial 的不确定标注仅提示复核范围。新证据能确认修改时，只用 start、end 切换对应片段的来源，'
                     '继续引用原候选其余区间。需要的末尾换行单独作为 text 项。整份候选经核对不适用时记录 rejected。'
                     '没有匹配的贡献或成果不足时，父核心可用现有工具自行完成。'
                     '以下是已启用贡献的用途索引：' + json.dumps(self.worker_options, ensure_ascii=False))
        if required:
            text += ('\n当前任务已有明确必验项：用 inspect_delivery 核对 requiredRebind/requiredPending。'
                     '覆盖沿用原 requirementId；target 完整复制返回值，expectedRevision 使用账本整数 revision。'
                     '真实 actionId 只支持对应版本和范围，未完成项须如实报告，不得删除或降级用户要求。')
        return text + self.github.instruction()

    def requirements(self, after=0):
        material = getattr(self.session, 'effective_requirements', None) or self.session.load().get('effectiveRequirements', {})
        from .task_input import requirements_page
        return requirements_page(material, after)

    def read(self, *, include_checks=True):
        from . import projects
        allowed = {r['id'] for r in projects.scope(self.session.dir.name)['resources']
                   if r['kind'] == 'code' and r['permission'] == 'read'}
        # Receipts retain their failures and versions. Executing a command does
        # not create a new user obligation, including after a later edit.
        return dict(ok=True, taskId=self.task.get('id'), demandRevision=self.task.get('revision'),
            issues={k: deepcopy(v) for k,v in self.state['issues'].items() if v['resourceId'] in allowed},
            checks={k: deepcopy(v) for k,v in self.state['checks'].items() if v['resourceId'] in allowed} if include_checks else {},
            checkCount=sum(v['resourceId'] in allowed for v in self.state['checks'].values()),
            versions={k:v for k,v in self.state['versions'].items() if k in allowed}, pending=[])

    def current_view(self, *, include_checks=True):
        """Compatibility inspection view; automatic feedback excludes check rows."""
        full = self.read(include_checks=include_checks)
        checks = list(full['checks'].values())
        selected, _ = self._latest_checks(full) if include_checks else ([],0)
        issues = []
        for key, row in full['issues'].items():
            if row.get('state') == 'closed' or self._unowned_check_issue(row):
                continue
            row['versionRelation'] = ('current' if row.get('revision') == full['versions'].get(row['resourceId'])
                                      else 'historical')
            row['attempts'] = [{k:str(v)[:360] for k,v in attempt.items()}
                               for attempt in row.get('attempts', [])[-1:]]
            issues.append((key, row))
        issues.sort(key=lambda pair: pair[1]['versionRelation'] != 'current')
        return dict(ok=True, taskId=full['taskId'], demandRevision=full['demandRevision'],
                    versions=full['versions'], checks=selected[:8], issues=dict(issues[:12]), pending=full['pending'],
                    history=dict(checkCount=full.get('checkCount',len(checks)), omittedOpenIssues=max(0,len(issues)-12),
                                 readTool='read_check_queue'))

    @staticmethod
    def _latest_checks(full):
        latest = {}
        checks = list(full['checks'].values())
        for sequence, row in enumerate(checks, 1):
            row['observationOrder'] = sequence
            row['versionRelation'] = ('current' if row['revision'] == full['versions'].get(row['resourceId'])
                                      else 'historical')
            key = (row['resourceId'], row.get('checkKey') or C.check_key(row), row.get('purpose','acceptance'))
            previous = latest.get(key)
            if previous is None or (row['versionRelation'] == 'current', sequence) > (
                    previous['versionRelation'] == 'current', previous['observationOrder']):
                latest[key] = row
        selected = sorted(latest.values(), key=lambda row: (
            row['versionRelation'] == 'current', row['observationOrder']), reverse=True)
        return selected, len(latest)

    def _unowned_check_issue(self, row):
        """Project verified legacy duplicates, not explicitly investigated defects."""
        check = self.state['checks'].get(row.get('latestActionId'), {})
        return (row.get('origin') in {None, 'check'}
                and not row.get('attempts') and row.get('signature') == '检查未通过'
                and check.get('resourceId') == row.get('resourceId')
                and bool(row.get('checkKey')) and check.get('checkKey') == row['checkKey'])

    def model_result(self, name, result, args=None):
        """Legacy caller adapter; the native loop uses the Tool contract directly."""
        from .tool_results import NATIVE_RENDERERS, legacy_json
        return NATIVE_RENDERERS.get(name,legacy_json)(result,args or {})

    def troubleshoot(self, resource_id, signature, hypothesis, result, state,
                     issue_id=None, evidence_action_id=None):
        if self.stop():
            raise W.Problem('cancelled', '执行已停止', 409)
        if (state not in {'investigating', 'blocked', 'resolved'} or
                any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 1200
                    for s in (signature, hypothesis, result))):
            raise W.Problem('invalid_troubleshooting', '记录具体问题、假设和实际结果')
        C.context_resource(self.root, resource_id)
        key = issue_id or hashlib.sha256(json.dumps([resource_id, signature]).encode()).hexdigest()[:24]
        old = self.state['issues'].get(key)
        if (issue_id and not old) or (old and old['resourceId'] != resource_id) or (state == 'resolved' and not old):
            raise W.Problem('unknown_issue', '更新或解决时使用本任务原 issue_id')
        row = deepcopy(old or dict(issueId=key, resourceId=resource_id, signature=evidence.redact(signature), attempts=[]))
        row['origin'] = 'agent'
        row.setdefault('purpose', 'acceptance')
        proof = self.state['checks'].get(evidence_action_id, {})
        if evidence_action_id and proof.get('resourceId') != resource_id:
            raise W.Problem('evidence_binding_mismatch', '证据需来自本任务同一资源的实际检查')
        if proof and state != 'resolved':
            # Preserve the original scope when an observed failure is explicitly
            # promoted to a problem; an unrelated success must not resolve it.
            for field in ('argv','checkKey','environment'):
                row.setdefault(field, proof.get(field))
            row['latestActionId'] = evidence_action_id
            row['revision'] = proof['revision']
        # resolved 先记为自述；只有同资源、当前版本、同命令/环境和用途的通过证据才能真正关闭问题。
        if state == 'resolved':
            valid = (proof.get('status') == 'passed' and proof.get('revision') == self.state['versions'].get(resource_id)
                     and (not row.get('argv') or row['argv'] == proof.get('argv'))
                     and (not row.get('checkKey') or row['checkKey'] == proof.get('checkKey'))
                     and row.get('purpose','acceptance') == proof.get('purpose','acceptance')
                     and (not row.get('environment') or row['environment'] == proof.get('environment')))
            row['state'] = 'closed' if valid else 'awaiting_verification'
            row['resolvedClaim'] = evidence.redact(result)
            if valid:
                row['closure'] = dict(actionId=evidence_action_id, revision=proof['revision'])
        else:
            row['state'] = state
        row['attempts'] = [*row['attempts'], evidence.redact(dict(hypothesis=hypothesis, result=result))][-8:]
        self.state['issues'][key] = row
        self.save()
        return dict(ok=True, issue_id=key, issue=deepcopy(row))

    def replace_entry(self, failed_action_id, passed_action_id, reason):
        """Reuse receipt validation without starting a coordinator or another loop."""
        from .execution_completion import validate_entry_replacement
        if self.stop():raise W.Problem('cancelled','执行已停止',409)
        f=self.state['checks'].get(failed_action_id);p=self.state['checks'].get(passed_action_id)
        if not f or not p or f['resourceId']!=p['resourceId']:
            raise W.Problem('check_replacement_denied','需本任务同资源的真实检查回执',409)
        thread,_,ws,_=C.context_resource(self.root,f['resourceId'])
        if thread['id']!=self.session.dir.name:
            raise W.Problem('wrong_source','检查不属于当前会话',403)
        with C.database() as db:
            rows=[json.loads(raw) for raw, in db.execute(
                "SELECT data FROM operations WHERE thread=? AND json_extract(data,'$.source.actionId') IN (?,?)",
                (thread['id'],failed_action_id,passed_action_id))]
        def find(aid,record):
            return next((r for r in rows if r.get('source',{}).get('actionId')==aid
                and r['source'].get('runId')==record.get('runId')
                and r['source'].get('role','executor')=='executor'),None)
        failed,passed=find(failed_action_id,f),find(passed_action_id,p)
        scripts=[]
        for aid,record in [(failed_action_id,f),(passed_action_id,p)]:
            if record.get('scriptHash'):
                value=evidence.detail(thread['id'],aid,C.RuntimeView())
                args=value.get('input') or {}
                script=args.get('script')
                scripts.append(script if isinstance(script,str) and not args.get('truncated')
                    and W.digest(script.encode())==record['scriptHash'] else None)
            else:scripts.append(None)
        replacement=validate_entry_replacement(failed,passed,W.current_revision(ws),reason,*scripts)
        failed['entryReplacement']=replacement
        with C.database() as db:
            db.execute('UPDATE operations SET data=? WHERE thread=? AND id=?',
                (json.dumps(failed),thread['id'],failed['id']))
        f['entryReplacement']=replacement
        for row in self.state['issues'].values():
            if row.get('latestActionId')==failed_action_id and row.get('origin','check')=='check':
                row.update(state='closed',closure=dict(kind='equivalent_entry_passed',**replacement))
        self.save()
        return dict(ok=True,originalFailureRetained=True,replacement=replacement)

    def observe(self, jobs, results):
        changed = False
        written = {job['args'].get('resource_id') for job in jobs
            if job['name'] in {'edit_project_file','write_project_file','create_project_file','delete_project_file'}
            and (results.get(job['i'], ({}, 0))[0] or {}).get('ok') is not False}
        # First account for every write in this batch; an earlier check cannot
        # close a problem against a later version in the same response.
        for job in jobs:
            out = results.get(job['i'], ({}, 0))[0] or {}
            rid = job['args'].get('resource_id')
            revision = out.get('projectRevisionAfter') or out.get('checkRevision') or out.get('projectRevision')
            if job['name'] in {'edit_project_file', 'write_project_file', 'create_project_file', 'delete_project_file'}:
                revision = revision or out.get('fileChangeRevision') or ('pending:' + str(job.get('action_id')))
            current = (out.get('ok') is not False if job['name'] != 'run_project_check' else
                       rid not in written and bool(out.get('projectRevisionAfter')))
            if rid and revision and current:
                self.state['versions'][rid] = revision
                changed = True
        for job in jobs:
            out = results.get(job['i'], ({}, 0))[0] or {}
            rid = job['args'].get('resource_id')
            if job['name']=='run_project_check' and out.get('executionEnvironment') and rid:
                self.state.setdefault('environmentObservations',{})[rid]=dict(
                    lastObserved=deepcopy(out['executionEnvironment']),
                    sourceWorkspaceId=(out.get('process') or {}).get('workspaceId'),
                    environmentId=(out.get('process') or {}).get('environmentId'),
                    sourceRevision=(out.get('verification') or {}).get('revision'),
                    availability='last_observed_not_reprobed')
                changed=True
            if job['name']=='review_test_environment' and out.get('released') and rid:
                row=self.state.get('environmentObservations',{}).get(rid,{})
                if row.get('environmentId')==out['released']:
                    self.state['environmentObservations'].pop(rid)
                    changed=True
            v = out.get('verification') or {}
            rid, action = job['args'].get('resource_id'), job.get('action_id')
            if not rid or not action or not v.get('revision'):
                continue
            self.state['versions'].setdefault(rid, v['revision'])
            check = dict(resourceId=rid, revision=v['revision'], status=v.get('status'), actionId=action,
                operationId=out.get('operationId'),
                runId=self.session.run_id, processId=(out.get('process') or {}).get('id'),
                argv=job['args'].get('argv') or out.get('argv') or (out.get('process') or {}).get('argv'),
                checkKey=out.get('checkKey'), purpose=job['args'].get('purpose','acceptance'),
                environment=out.get('environment'), material=job['args'].get('material'),
                scriptHash=W.digest(job['args']['script'].encode()) if isinstance(job['args'].get('script'),str) else out.get('scriptHash'),
                diagnostics=evidence.redact(out.get('diagnostics') or {}))
            check['label'] = out.get('checkLabel') or C.check_label(check.get('argv'),
                script=job['args'].get('script'), scope_reason=job['args'].get('scope_reason'))
            if not check['checkKey']:
                check['checkKey'] = C.check_key(dict(check, retain_output=job['args'].get('retain_output'),
                    writable_copy=job['args'].get('writable_copy',False), capture_changes=job['args'].get('capture_changes',False)))
            self.state['checks'][action] = check
            if check['status'] == 'passed' and v['revision'] == self.state['versions'].get(rid):
                for row in self.state['issues'].values():
                    if (row['resourceId'] == rid and row.get('argv') == check['argv']
                            and row.get('purpose','acceptance') == check['purpose']
                            and (row.get('checkKey') == check['checkKey'] or
                                 not row.get('checkKey') and not (set(job['args']) & {'script','material','script_operation_id'})
                                 and row.get('purpose','acceptance') == check['purpose']
                                 and row.get('environment') == check['environment'])):
                        row.update(state='closed', closure=dict(actionId=action, revision=v['revision']))
            changed = True
        if changed:
            self.save()

    def feedback(self, messages):
        self._background_feedback(messages)
        material = self.current_view(include_checks=False)
        previous = any(isinstance(m.content,str) and m.content.startswith('[context-update:executor-work:')
            and any(kind=='runtimeUpdate' for kind,_ in m.context_parts) for m in messages)
        if material['issues'] or material['versions'] or previous:
            # Raw receipts stay in tool history and explicit read_check_queue.
            # Empty checks also clears a retained legacy state table by delta.
            issues = {key:{k:row[k] for k in
                ('resourceId','revision','state','label','signature','latestActionId','attempts','purpose','origin') if k in row}
                for key,row in material['issues'].items()}
            append_state_update(messages, 'executor-work', dict(taskId=material['taskId'],
                demandRevision=material['demandRevision'], versions=material['versions'],
                checks={}, issues=issues, pending={p['actionId']:{k:p[k] for k in
                    ('resourceId','revision','label')} for p in material['pending']},
                history=material['history']))

    def _background_feedback(self, messages):
        from . import check_sessions
        from .tool_results import check_receipt, terminal_check_version
        from . import projects
        from .working_context import fold_state_updates
        previous = fold_state_updates(m.content for m in messages
            if isinstance(m.content,str) and m.content.startswith('[context-update:background-checks:')
            and any(kind=='runtimeUpdate' for kind,_ in m.context_parts)) or {}
        # 晚到检查按逻辑任务及当前资料范围过滤；旧轮次的原始回执可保留，但不能扩大读取授权。
        retained = previous.get('checks',{}) if previous.get('taskId') == self.task.get('id') else {}
        captured = {r['id']:r for r in projects.conversation(self.session.dir.name).get('resourceSnapshot',{}).get('resources',[])}
        allowed = {r['id'] for r in projects.scope(self.session.dir.name)['resources']
            if r['kind']=='code' and r['permission']=='read' and captured.get(r['id'],{}).get('path')==r['path']}
        facts = {}
        for op in check_sessions.completed(self.session.dir.name, self.task.get('id')):
            if op['resourceId'] not in allowed:
                continue
            out = check_sessions.result(op)
            aid = op['source']['actionId']
            if not op.get('backgroundYielded') and aid in self.state['checks']:
                continue
            stamp = W.digest(json.dumps(out, sort_keys=True, ensure_ascii=False).encode())
            if self.state['checks'].get(aid, {}).get('resultVersion') != stamp:
                args = op.get('backgroundArgs') or dict(op['target'], resource_id=op['resourceId'])
                self.observe([dict(name='run_project_check', args=args, action_id=aid, i=0)], {0:(out,0)})
                if aid in self.state['checks']:
                    self.state['checks'][aid]['resultVersion'] = stamp
                    self.save()
            # A terminal tool response already in history is itself delivery.
            # Keep an existing runtime snapshot stable; don't remove/re-add it.
            view = check_receipt(out, dict(resource_id=op['resourceId']))
            version = terminal_check_version(view)
            if op['id'] not in retained and version:
                seen = False
                for message in messages:
                    if message.role != 'tool' or not isinstance(message.content,str) or op['id'] not in message.content:
                        continue
                    try:
                        value = json.loads(message.content)
                    except ValueError:
                        continue
                    if (isinstance(value,dict) and value.get('operationId')==op['id']
                            and terminal_check_version(value)==version):
                        seen=True
                        break
                if seen:
                    continue
            view['_harnessEvidence'] = dict(actionId=aid, runId=op['source']['runId'])
            facts[op['id']] = view
        if facts:
            append_state_update(messages, 'background-checks', dict(taskId=self.task.get('id'), checks=facts))

    def completion(self, status, verify, why=''):
        """One projection for the final text and the completion card."""
        from .execution_completion import overview
        work=self.read()
        checks=work['checks']
        op_actions={c['operationId']:aid for aid,c in checks.items() if c.get('operationId')}
        facts_checks=deepcopy((verify or {}).get('codeChecks') or {})
        for resource in facts_checks.get('resources',[]):
            for check in resource.get('checks',[]):
                if check.get('actionId'):op_actions[check.get('operationId')]=check['actionId']
            remaining=[]
            for gap in resource.get('pending',[]):
                aid=gap.get('actionId') or op_actions.get(gap.get('operationId'))
                if aid:op_actions[gap.get('operationId')]=aid
                check=checks.get(aid,{})
                if check.get('entryReplacement') or (gap.get('kind')=='recheck' and check.get('status')=='passed'):
                    continue
                remaining.append(gap)
            resource['pending']=remaining
            if resource.get('status')=='incomplete' and not remaining and not resource.get('checks'):
                resource['status']='unverified'
        facts = dict(runtimeCompletion=True, nativeExecutor=True, backgroundReviewer=False, state=status,
            executorChecks=facts_checks,
            executorAcceptance=(verify or {}).get('acceptance') or {},
            executorOutcome=dict(status=status, why=why))
        completion = overview(facts)
        # 同一动作可能同时出现在操作回执和原生检查视图中；按资源/动作合并展示，并保留各自引用来源。
        unique={}
        def add(row,source,aid=None,rid=None):
            key=(rid,aid) if aid else (row['kind'],row['id'])
            if key not in unique:unique[key]=dict(row,sources=[],references=[])
            item=unique[key]
            if source not in item['sources']:item['sources'].append(source)
            ref=dict(kind=row['kind'],id=row['id'])
            if ref not in item['references']:item['references'].append(ref)
        for row in completion['pending']:
            aid=op_actions.get(row['id']) if row['kind']=='check' else None
            add(row,'operation' if row['kind']=='check' else row['kind'],aid,checks.get(aid,{}).get('resourceId'))
        for gap in work['pending']:
            add(dict(kind='check',id=gap['actionId'],role='executor',state=gap['state'],text=gap['text']),
                'native',gap['actionId'],gap['resourceId'])
        for row in work['issues'].values():
            if row.get('state') != 'closed' and not self._unowned_check_issue(row):
                add(dict(kind='check', id=row['issueId'], role='executor',
                    state=row.get('state', 'unknown'), text=row['signature'][:240] + '：尚无有效解决证据'),
                    'issue',row.get('latestActionId') if row.get('origin','check')=='check' else None,row['resourceId'])
        completion['pending']=list(unique.values())
        if verify and not verify.get('verifierOk', verify.get('ok')) and not completion['pending']:
            completion['pending'].append(dict(kind='check', id='execution', role='executor', state='unverified',
                text=str(verify.get('reason') or '本轮检查未通过或证据不足')[:300]))
        if status == 'success' and completion['pending']:
            completion['label'] = '本轮已结束 · 仍有未验收事项'
        return completion

    def finish(self, status, verify, why=''):
        completion=self.completion(status,verify,why)
        self.session.save(executorWork=self.state, executorCompletion=dict(runId=self.session.run_id, completion=completion))
