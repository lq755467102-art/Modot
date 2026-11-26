"""Public-contract cases through package installation, native tools and records."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from loop import worker_plugins as P, worker_contract as C, plugins, subtasks as S, workbench as W
from loop import execution_policy as E, parallel_budget as B, projects, permissions, models, task_core_models
from loop.tools import set_call, reset_call, default_registry
from loop.native_entry import NativeEntry
from loop.session import Session
from tools.test_conversations import ConversationFixture


def manifest():
    shape=dict(type='object',properties=dict(note=dict(type='string',maxLength=1000)),required=['note'],additionalProperties=False)
    return dict(name='worker-contract-fixture',version='0.1.0',workers=[dict(name='fixture',protocol=C.PROTOCOL,
        description='Conformance fixture; not an official assistant',method='METHOD_PRIVATE: report only the delegated facts.',
        inputSchema=shape,outputSchema=shape,requires=['native-l0','independent-history','structured-result'],optional=['future-feature'])])


class WorkerContractTests(ConversationFixture,unittest.TestCase):
    def setup_worker(self, *, model_role=None, transcription_project=False):
        package=self.root/'package';package.mkdir()
        package_manifest = manifest()
        if model_role:
            package_manifest['workers'][0]['modelRole'] = model_role
        if transcription_project:
            contribution = package_manifest['workers'][0]
            contribution['outputSchema'] = dict(type='object', properties={'transcription': {'type': 'string'}},
                                                required=['transcription'], additionalProperties=False)
            contribution['requires'].append('read-code')
        (package/'plugin.json').write_text(json.dumps(package_manifest), encoding='utf8')
        installed=plugins.install(str(package),plugins.preview(str(package))['fingerprint'])
        self.plugin=plugins.change(installed['id'],dict(expectedRevision=installed['revision'],workersEnabled=True,confirm=True))
        if transcription_project:
            self.code = self.root / 'transcription'
            self.code.mkdir()
            def project_request(path, body):
                status, reply = W.handle('POST', '/api/v2/projects'+path, {}, body, self.sv)
                self.assertLess(status, 300, reply)
                return reply
            project = project_request('', {'name': 'Transcription fixture'})
            base = '/'+project['id']+'/resources'
            resource = project_request(base, {'path': str(self.code), 'kind': 'code'})
            self.resource_id = resource['id']
            project_request(base+'/'+self.resource_id+'/permission',
                            dict(permission='read', write=True, execute=False, confirm=True))
            _, parent = self.call('POST', body={'projectId': project['id']})
            status, reply = self.call('POST', '/'+parent['id']+'/scope',
                                      {'projectId': project['id'], 'resourceIds': [self.resource_id]})
            self.assertLess(status, 300, reply)
        else:
            _, parent = self.call('POST')
        self.parent = parent['id']
        self.d = self.sv._resolve_sid(self.parent)
        self.run=W.RT.register(self.d,os.getpid())['run_id']
        self.task='logical-'+self.parent
        W.atomic_json(self.d/'session.json',dict(taskInput=dict(id=self.task,revision=2),msgs=[]))
        projects.snapshot(self.parent)
        self.budget=self.d/'execution-runs'/(self.run+'.json')
        W.atomic_json(self.budget,dict(runId=self.run,config=dict(E.defaults(),modelCalls=0,maxTurns=0,contextLimit=131072),usedCalls=0,byRole={}))
        permissions.freeze(self.run,self.parent)
        self.args=dict(worker=self.plugin['id']+'/fixture',request_key='one',input={'note':'INPUT_PRIVATE'},resource_ids=[],kind='report')

    def parent_call(self,fn,*args,tool='manage_plugin_worker',**kw):
        token=set_call(dict(sessionId=self.parent,runId=self.run,actionId='a'*32,tool=tool,
                            role='executor', logicalTaskId=self.task, demandRevision=2))
        try:return fn(*args,**kw)
        finally:reset_call(token)

    def delegate(self,**changes):
        with patch.object(S,'_schedule_locked'):
            out=self.parent_call(P.delegate,self.parent,dict(self.args,**changes),self.sv,tool='delegate_plugin_worker')
        return S.load(self.parent,out['delegation_id'])

    def running(self,row):
        S._prepare(row,self.sv);row=S.load(self.parent,row['id'])
        child=self.sv._resolve_sid(row['childId']);run=W.RT.register(child,os.getpid())['run_id']
        row.update(state='running',childRunId=run);S.save(row)
        W.atomic_json(child/'session.json',dict(run_id=run,status='running',msgs=[dict(role='user',content='OWN_HISTORY')]))
        return row

    def publish(self,row,data=None,key='one'):
        token=set_call(dict(sessionId=row['childId'],runId=row['childRunId'],actionId='b'*32,tool='submit_worker_result'))
        try:return P.publish(data or {'note':'RESULT_PRIVATE'},key)
        finally:reset_call(token)

    def manage(self,row,action,**kw):
        return self.parent_call(P.manage,self.parent,dict(dict(delegation_id=row['id'],action=action),**kw),self.sv)

    def test_write_uses_the_delegated_transcription_verbatim(self):
        self.setup_worker(transcription_project=True)
        worker = self.running(self.delegate(resource_ids=[self.resource_id]))
        saved = self.publish(worker, {'transcription': 'RIGHT-N07'})
        reading = self.manage(worker, 'result', result_id=saved['result_id'])
        writer = default_registry(W.workspace(projects.conversation(self.parent)['workspaceId'])['path'])
        reply = self.parent_call(
            writer.get('write_project_file').fn, resource_id=self.resource_id, relative_path='out.txt', create=True,
            parts=[{'text': 'left-'}, {'delegation_id': reading['delegation_id'], 'result_id': reading['result_id']},
                   {'text': '\n'}],
            tool='write_project_file',
        )
        self.assertTrue(reply['ok'], reply)
        self.assertEqual((self.code / 'out.txt').read_bytes(), b'left-RIGHT-N07\n')
        self.assertEqual([x['resultId'] for x in reply['resultSources']], [reading['result_id']])

    def test_retyped_but_changed_transcription_requires_explicit_parts(self):
        self.setup_worker(transcription_project=True)
        worker = self.running(self.delegate(resource_ids=[self.resource_id]))
        transcription = 'AbCdEfGhIjKlMnOpQrStUvWxYz'
        saved = self.publish(worker, {'transcription': transcription})
        self.manage(worker, 'result', result_id=saved['result_id'])
        writer = default_registry(W.workspace(projects.conversation(self.parent)['workspaceId'])['path'])
        args = dict(resource_id=self.resource_id, relative_path='out.txt', create=True,
                    text=transcription[:7] + '?' + transcription[8:17] + 'XY' + transcription[19:] + '\n')
        changed = self.parent_call(writer.get('write_project_file').fn, tool='write_project_file', **args)
        self.assertEqual(changed.get('code'), 'transcription_reference_required', changed)
        self.assertFalse((self.code / 'out.txt').exists())

    def test_accepted_transcription_is_the_default_body_and_records_its_source(self):
        self.setup_worker(transcription_project=True)
        worker = self.running(self.delegate(resource_ids=[self.resource_id]))
        saved = self.publish(worker, {'transcription': 'Letter-Qr87\n'})
        read = self.manage(worker, 'result', result_id=saved['result_id'])
        self.assertEqual(read['writeParts'], [{'delegation_id': worker['id'], 'result_id': saved['result_id']}])
        self.manage(worker, 'receipt', request_key='adopt', result_id=saved['result_id'], disposition='accepted')
        writer = default_registry(W.workspace(projects.conversation(self.parent)['workspaceId'])['path'])
        reply = self.parent_call(
            writer.get('write_project_file').fn, resource_id=self.resource_id, relative_path='result.txt',
            create=True, tool='write_project_file')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual((self.code / 'result.txt').read_bytes(), b'Letter-Qr87\n')
        self.assertEqual([r['resultId'] for r in reply['resultSources']], [saved['result_id']])

    def test_exact_copy_records_parts_without_changing_the_proposed_newline(self):
        self.setup_worker(transcription_project=True)
        worker = self.running(self.delegate(resource_ids=[self.resource_id]))
        saved = self.publish(worker, {'transcription': 'Case-sensitive-05'})
        self.manage(worker, 'result', result_id=saved['result_id'])
        writer = default_registry(W.workspace(projects.conversation(self.parent)['workspaceId'])['path'])
        write = writer.get('write_project_file').fn
        reply = self.parent_call(write, resource_id=self.resource_id, relative_path='result.txt', create=True,
                                 text='Case-sensitive-05\n', tool='write_project_file')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual((self.code / 'result.txt').read_bytes(), b'Case-sensitive-05\n')
        self.assertEqual([r['resultId'] for r in reply['resultSources']], [saved['result_id']])
        notes = self.parent_call(write, resource_id=self.resource_id, relative_path='notes.txt', create=True,
                                 text='Written about a different task.\n', tool='write_project_file')
        self.assertTrue(notes['ok'], notes)
        self.assertNotIn('resultSources', notes)

    def test_write_can_select_a_rechecked_character_without_retyping_other_parts(self):
        self.setup_worker(transcription_project=True)
        worker = self.running(self.delegate(resource_ids=[self.resource_id]))
        original = self.publish(worker, {'transcription': 'right-n07'})
        correction = self.publish(worker, {'transcription': 'N'}, key='rechecked')
        for selected in (original, correction):
            self.manage(worker, 'result', result_id=selected['result_id'])
        writer = default_registry(W.workspace(projects.conversation(self.parent)['workspaceId'])['path'])
        initial = {'delegation_id': worker['id'], 'result_id': original['result_id']}
        chosen = {'delegation_id': worker['id'], 'result_id': correction['result_id']}
        reply = self.parent_call(
            writer.get('write_project_file').fn, resource_id=self.resource_id, relative_path='out.txt', create=True,
            parts=[dict(initial, start=0, end=6), chosen, dict(initial, start=7, end=9)],
            tool='write_project_file',
        )
        self.assertTrue(reply['ok'], reply)
        self.assertEqual((self.code / 'out.txt').read_bytes(), b'right-N07')
        self.assertEqual([(item['resultId'], item['sourceStart'], item['sourceEnd']) for item in reply['resultSources']],
                         [(original['result_id'], 0, 6), (correction['result_id'], 0, 1), (original['result_id'], 7, 9)])

    def test_write_requires_a_current_result_that_has_not_been_rejected(self):
        self.setup_worker(transcription_project=True)
        worker = self.running(self.delegate(resource_ids=[self.resource_id]))
        result = self.publish(worker, {'transcription': 'RIGHT-N07'})
        result_id = result['result_id']
        self.manage(worker, 'result', result_id=result_id)
        writer = default_registry(W.workspace(projects.conversation(self.parent)['workspaceId'])['path'])
        write = writer.get('write_project_file').fn
        args = dict(resource_id=self.resource_id, relative_path='out.txt', create=True,
                    parts=[{'delegation_id': worker['id'], 'result_id': result_id}])

        self.manage(worker, 'receipt', request_key='reject-reading', result_id=result_id, disposition='rejected')
        rejected = self.parent_call(write, tool='write_project_file', **args)
        self.assertEqual(rejected.get('code'), 'worker_result_rejected', rejected)

        self.manage(worker, 'receipt', request_key='corrected-decision', result_id=result_id, disposition='accepted')
        token = set_call(dict(sessionId=self.parent, runId=self.run, actionId='a'*32, role='executor',
                              logicalTaskId=self.task, demandRevision=3, tool='write_project_file'))
        try:
            outdated = write(**args)
        finally:
            reset_call(token)
        self.assertEqual(outdated.get('code'), 'worker_result_scope', outdated)
        self.assertFalse((self.code / 'out.txt').exists())

    def test_manifest_negotiation_and_disabled_registration(self):
        self.setup_worker()
        self.assertEqual(P.discover(self.parent)['workers'][0]['unavailableOptional'],['future-feature'])
        for change,code in [({'protocol':'wrong'},'worker_protocol'),({'requires':['recursive-delegation']},'worker_capability'),
                            ({'inputSchema':{'type':'object','$ref':'remote'}},'worker_schema')]:
            item=dict(manifest()['workers'][0],**change)
            with self.subTest(change=change),self.assertRaises(W.Problem) as caught:C.declarations([item])
            self.assertEqual(caught.exception.code,code)
        plugins.change(self.plugin['id'],dict(expectedRevision=self.plugin['revision'],workersEnabled=False))
        self.assertEqual(P.discover(self.parent)['workers'],[])


    def test_visual_core_batches_distinct_views_and_publishes_before_another_turn(self):
        from base64 import b64decode
        from hashlib import sha256
        from PIL import Image, ImageDraw
        from loop.executor_core import ExecutorCore
        from loop.provider import OpenAICompatProvider

        self.setup_worker(transcription_project=True)
        image = Image.new('RGB', (40, 20), 'white')
        ImageDraw.Draw(image).rectangle((4, 3, 13, 10), fill='black')
        image.save(self.code / 'panel.png')

        requests = []

        def wire(_provider, request, _stop):
            requests.append(json.loads(request.data))
            result = dict(transcription='Ax' if len(requests) == 1 else 'AX',
                          uncertainties=[dict(position='2', alternatives=['x', 'X'], reason='大小写待复核')])
            chunk = dict(choices=[dict(delta=dict(content=json.dumps(result)), finish_reason='stop')])
            yield b'data: ' + json.dumps(chunk).encode() + b'\n'
            yield b'data: [DONE]\n'

        with (patch.object(models, 'path', return_value=self.root / 'models.json'),
              patch.object(models, '_creds', return_value={}),
              patch.object(OpenAICompatProvider, '_iter_lines', wire)):
            main = models.user_action(dict(act='save', name='Main', base_url='https://fixture.example/v1',
                                            model='main-fixture', api_key='main-fixture-key'))['id']
            self.assertTrue(models.user_action(dict(act='select', role='main', id=main))['ok'])
            visual = models.user_action(dict(act='save', name='Vision', base_url='https://fixture.example/v1',
                                             model='vision-fixture', api_key='vision-fixture-key'))['id']
            self.assertTrue(models.user_action(dict(act='select', role='vision', id=visual))['ok'])
            preset = plugins.vision_preset()
            enabled = plugins.vision_preset(dict(enabled=True, fingerprint=preset['fingerprint'],
                                                 expectedRevision=preset['revision']))
            projects.snapshot(self.parent)
            row = self.running(self.delegate(worker=enabled['id'] + '/transcribe',
                               input=dict(source_path='panel.png', goal='Read the panel'),
                               resource_ids=[self.resource_id]))
            child = Session(self.sv._resolve_sid(row['childId']))
            child.run_id = row['childRunId']
            projects.snapshot(row['childId'])
            permissions.freeze(row['childRunId'], row['childId'])
            project_root = W.workspace(projects.conversation(row['childId'])['workspaceId'])['path']
            registry = default_registry(Path(project_root))
            ExecutorCore(child, Path(project_root)).install(registry)
            tool = registry.get('transcribe_image_batch')
            self.assertIsNotNone(tool)
            self.assertNotIn('read_image', registry.names())
            self.assertNotIn('transcribe_image', registry.names())

            full = dict(path='panel.png')
            crop = dict(path='panel.png', view=dict(crop=[0, 0, 0.5, 1]))
            token = set_call(dict(sessionId=row['childId'], runId=row['childRunId'],
                                  actionId='b' * 32, tool=tool.name))
            try:
                first = tool(dict(images=[full, full, crop]))
                self.assertTrue(first['ok'], first)
                saved = S.load(self.parent, row['id'])['worker']['results'][0]
                self.assertEqual(saved['result_id'], first['result_id'])
                self.assertEqual(saved['data']['transcription'], 'Ax')
                content = requests[0]['messages'][0]['content']
                hashes = [sha256(b64decode(p['image_url']['url'].split(',', 1)[1])).hexdigest()
                          for p in content if p['type'] == 'image_url']
                self.assertEqual(len(hashes), 2)
                self.assertEqual(len(set(hashes)), 2)
                self.assertEqual([e['image_sha256'] for e in saved['data']['evidence']], hashes)

                second = tool(dict(images=[crop]))
                self.assertTrue(second['ok'], second)
                self.assertEqual(second['transcription'], 'AX')
                self.assertIn('Ax', requests[1]['messages'][0]['content'][0]['text'])
                third = tool(dict(images=[crop]))
                self.assertEqual(third['code'], 'vision_batch_limit')
                self.assertEqual(len(requests), 2)
            finally:
                reset_call(token)

    def test_task_core_binding_stops_requests_when_the_selected_model_changes(self):
        def connect(name, base, model, credential):
            return models.user_action(dict(act='save', name=name, base_url=base,
                                           model=model, api_key=credential))['id']

        self.setup_worker(model_role='vision')
        with (patch.object(models, 'path', return_value=self.root / 'models.json'),
              patch.object(models, '_creds', return_value={})):
            main = connect('main', 'https://api.deepseek.com/v1', 'fixture-main', 'main-fixture-key')
            visual = connect('visual', 'https://open.bigmodel.cn/api/paas/v4',
                             'fixture-vision', 'vision-fixture-key')
            self.assertTrue(models.user_action(dict(act='select', role='main', id=main))['ok'])
            self.assertTrue(models.user_action(dict(act='select', role='vision', id=main))['ok'])
            unavailable, = P.discover(self.parent)['workers']
            self.assertEqual(unavailable['core_type'], 'task_core')
            self.assertEqual(unavailable['reason'], 'worker_model_same')
            self.assertFalse(unavailable['available'])
            with self.assertRaises(W.Problem) as same:
                self.delegate()
            self.assertEqual(same.exception.code, 'worker_model_same')

            self.assertTrue(models.user_action(dict(act='select', role='vision', id=visual))['ok'])
            # An environment endpoint must not capture the key from a saved selection.
            with patch.dict(os.environ, {'HARNESS_VISION_BASE_URL': 'https://other.example/v1',
                                         'HARNESS_VISION_MODEL': 'another-model'}):
                row = self.running(self.delegate())
                connection = task_core_models.provider_for_child(row['childId'])
                outbound = connection._request({'model': connection.model})
            self.assertEqual(outbound.full_url, 'https://open.bigmodel.cn/api/paas/v4/chat/completions')
            self.assertEqual(outbound.get_header('Authorization'), 'Bearer vision-fixture-key')
            self.assertEqual(connection.model, 'fixture-vision')
            self.assertNotIn('fixture-key', json.dumps(row))
            self.assertEqual(P.public(row)['model']['model'], 'fixture-vision')
            self.assertIsNone(task_core_models.provider_for_child(self.parent))
            submitted = self.publish(row, data={'note': 'Verified reading'})

            self.assertTrue(models.user_action(dict(act='select', role='vision', id=main))['ok'])
            with self.assertRaises(W.Problem) as revoked:
                connection._request({'model': connection.model})
            self.assertEqual(revoked.exception.code, 'worker_model_changed')
            result = self.manage(row, 'result', result_id=submitted['result_id'])
            self.assertEqual(result['data']['note'], 'Verified reading')

    def test_untyped_worker_keeps_the_original_model_path(self):
        self.setup_worker()
        row = self.running(self.delegate())
        self.assertEqual(P.public(row)['core_type'], 'sub_agent')
        self.assertIsNone(task_core_models.provider_for_child(row['childId']))

    def test_atomic_delegation_dedup_and_independent_histories(self):
        self.setup_worker()
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows=list(pool.map(lambda _:self.delegate(),range(2)))
        self.assertEqual(rows[0]['id'],rows[1]['id']);self.assertEqual(len(S.listing(self.parent)),1)
        with self.assertRaises(W.Problem):self.delegate(input={'note':'different'})
        other=self.delegate(request_key='two')
        self.assertNotEqual(other['childId'],rows[0]['childId'])
        self.assertEqual(P.public(other)['logical_task_id'],self.task)
        self.assertEqual(S.execution_config(other)['contextLimit'],131072)

    def test_input_schema_and_identity_reject(self):
        self.setup_worker()
        for value in ({'extra':1},{'note':1},{'note':'a','extra':True}):
            with self.subTest(value=value),self.assertRaises(W.Problem):self.delegate(input=value)
        row=self.delegate()
        with self.assertRaises(W.Problem):self.manage(row,'result',result_id=row['worker']['operationId'])
        with self.assertRaises(W.Problem):self.manage(row,'query',delegation_id='wop-'+row['id'])

    def test_result_persistence_delivery_and_disposition_are_separate(self):
        self.setup_worker();row=self.running(self.delegate())
        result=self.publish(row)
        self.assertEqual(result['result_id'],self.publish(row)['result_id'])
        with self.assertRaises(W.Problem):self.publish(row,{'note':'changed'})
        saved=S.load(self.parent,row['id'])['worker']['results'][0]
        self.assertEqual(saved['delivery'],{});self.assertIsNone(saved['disposition'])
        session=SimpleNamespace(dir=self.d,run_id=self.run,worker_contract=True)
        msgs=[];P.updates(session,msgs)
        self.assertEqual(len(msgs),1)
        # Crash before an actual request: unacknowledged notification is replayable.
        replay=[];P.updates(session,replay);self.assertEqual(msgs[0].content,replay[0].content)
        P.included(session,dict(messages=[m.dump() for m in msgs]),'snapshot-one')
        replay=[];P.updates(session,replay);self.assertEqual(replay,[])
        read=self.manage(row,'result',result_id=result['result_id'])
        self.assertEqual(read['data']['note'],'RESULT_PRIVATE');self.assertIsNone(read['disposition'])
        self.assertEqual(read['delivery']['notifiedIn'],'snapshot-one')
        self.assertNotIn('includedIn',read['delivery'])
        P.included(session,dict(messages=[dict(role='tool',content=json.dumps(dict(read,data={'note':'truncated'})))]),'snapshot-fragment')
        self.assertNotIn('includedIn',S.load(self.parent,row['id'])['worker']['results'][0]['delivery'])
        P.included(session,dict(messages=[dict(role='tool',content=json.dumps(read))]),'snapshot-body')
        self.assertEqual(S.load(self.parent,row['id'])['worker']['results'][0]['delivery']['includedIn'],'snapshot-body')
        receipt=dict(result_id=result['result_id'],request_key='accept',disposition='partial')
        self.manage(row,'receipt',**receipt)
        self.assertTrue(self.manage(row,'receipt',**receipt)['duplicate'])
        self.assertEqual(S.load(self.parent,row['id'])['worker']['results'][0]['disposition']['value'],'partial')

    def test_message_boundary_and_duplicate(self):
        self.setup_worker();row=self.running(self.delegate())
        # Hold a known worker identity so a state query doesn't pretend this test
        # process is the real backend observer.
        with patch.dict(S._workers,{row['id']:object()}):
            self.manage(row,'message',request_key='message',text='MESSAGE_ONE')
            self.assertTrue(self.manage(row,'message',request_key='message',text='MESSAGE_ONE')['duplicate'])
        child=SimpleNamespace(dir=self.sv._resolve_sid(row['childId']),worker_contract=True)
        msgs=[];P.updates(child,msgs);P.updates(child,msgs);self.assertEqual(len(msgs),1)
        self.assertIn('MESSAGE_ONE',msgs[0].content)
        P.included(child,dict(messages=[m.dump() for m in msgs]),'child-snapshot')
        self.assertEqual(S.load(self.parent,row['id'])['worker']['messages'][0]['includedIn'],'child-snapshot')

    def test_resume_reuses_directory_and_history_without_resetting_budget(self):
        self.setup_worker();row=self.running(self.delegate());self.publish(row)
        d=self.sv._resolve_sid(row['childId']);state=W.read(d/'session.json');state.update(status='needs_human',waitQuestion='which?')
        W.atomic_json(d/'session.json',state);runtime=W.RT.read(d);runtime.update(pid=0,identity=None);W.atomic_json(d/'runtime.json',runtime)
        B.consume(self.budget,'executor',row['id'])
        with patch.object(S,'schedule') as scheduled:
            reply=self.manage(row,'resume',request_key='resume-one',text='Use the original scope')
            again=self.manage(row,'resume',request_key='resume-one',text='Use the original scope')
        self.assertEqual(reply['child_session_id'],row['childId']);self.assertEqual(again['operation_id'],reply['operation_id'])
        self.assertNotEqual(reply['operation_id'],row['worker']['operationId']);scheduled.assert_called_once()
        current=S.load(self.parent,row['id']);self.assertEqual(current['derivedProjectId'],row['derivedProjectId'])
        self.assertEqual(W.read(d/'session.json')['msgs'][0]['content'],'OWN_HISTORY')
        self.assertEqual(B._read(self.budget)['allocations'][row['id']]['used'],1)

    def test_crash_reconciles_result_but_never_replays_side_effect(self):
        self.setup_worker();row=self.running(self.delegate());self.publish(row)
        d=self.sv._resolve_sid(row['childId']);runtime=W.RT.read(d);runtime.update(pid=0,identity=None);W.atomic_json(d/'runtime.json',runtime)
        with patch.object(S,'schedule') as start:
            self.assertEqual(self.manage(row,'query')['state'],'unknown');self.assertEqual(self.manage(row,'query')['state'],'unknown')
            start.assert_not_called()
        self.assertEqual(len(self.manage(row,'query')['results']),1)
        state=W.read(d/'session.json');state.update(status='success',final='finished');W.atomic_json(d/'session.json',state)
        self.assertEqual(self.manage(row,'query')['state'],'completed')

    def test_revocation_blocks_old_handles_but_retains_results(self):
        self.setup_worker();row=self.running(self.delegate());result=self.publish(row)
        plugins.change(self.plugin['id'],dict(expectedRevision=self.plugin['revision'],workersEnabled=False))
        with self.assertRaises(W.Problem):S.validate_child(row['childId'])
        with self.assertRaises(W.Problem):self.publish(row,key='after-revoke')
        with self.assertRaises(W.Problem):self.manage(row,'message',request_key='later',text='forbidden')
        self.assertEqual(self.manage(row,'result',result_id=result['result_id'])['data']['note'],'RESULT_PRIVATE')

    def test_cancel_is_idempotent_and_not_resurrected(self):
        self.setup_worker();row=self.delegate()
        # Unstarted queue is still owned by its dispatcher.
        with patch.dict(S._workers,{row['id']:object()}):
            self.assertEqual(self.manage(row,'cancel')['state'],'cancelled')
            self.assertEqual(self.manage(row,'cancel')['state'],'cancelled')
        with self.assertRaises(W.Problem):self.manage(row,'resume',request_key='after-cancel',text='continue')

    def test_cancel_after_crash_releases_only_after_confirmed_exit(self):
        self.setup_worker();row=self.running(self.delegate());self.publish(row)
        self.assertEqual(self.manage(row,'query')['state'],'unknown')
        self.assertEqual(self.manage(row,'cancel')['state'],'unknown')
        self.assertFalse(B._read(self.budget)['allocations'][row['id']]['terminal'])
        d=self.sv._resolve_sid(row['childId']);runtime=W.RT.read(d);runtime.update(pid=0,identity=None);W.atomic_json(d/'runtime.json',runtime)
        self.assertEqual(self.manage(row,'cancel')['state'],'cancelled')
        self.assertTrue(B._read(self.budget)['allocations'][row['id']]['terminal'])
        self.assertEqual(len(S.load(self.parent,row['id'])['worker']['results']),1)

    def test_owner_can_cancel_after_resource_read_grant_is_revoked(self):
        self.setup_worker();row=self.running(self.delegate())
        with patch.object(S,'audit_child',side_effect=W.Problem('resource_denied','revoked',403)):
            self.assertTrue(self.manage(row,'cancel')['stop_requested'])

    def test_resume_launch_gap_does_not_inherit_previous_terminal_state(self):
        self.setup_worker();row=self.running(self.delegate())
        d=self.sv._resolve_sid(row['childId']);state=W.read(d/'session.json');state.update(status='success');W.atomic_json(d/'session.json',state)
        runtime=W.RT.read(d);runtime.update(pid=0,identity=None);W.atomic_json(d/'runtime.json',runtime)
        with patch.object(S,'schedule'):
            self.manage(row,'resume',request_key='new-attempt',text='continue')
        self.assertEqual(self.manage(row,'query')['state'],'unknown')
        self.assertIsNone(S.load(self.parent,row['id']).get('childRunId'))

    def test_failed_resume_save_does_not_leave_a_new_budget_reservation(self):
        self.setup_worker();row=self.running(self.delegate())
        d=self.sv._resolve_sid(row['childId']);runtime=W.RT.read(d);runtime.update(pid=0,identity=None);W.atomic_json(d/'runtime.json',runtime)
        S.finish_task(self.parent,row['id'],'completed','fixture')
        with patch.object(S,'save',side_effect=OSError('fixture: durable write failed')),self.assertRaises(OSError):
            self.manage(row,'resume',request_key='new',text='continue')
        self.assertTrue(B._read(self.budget)['allocations'][row['id']]['terminal'])
        self.assertEqual(S.load(self.parent,row['id'])['state'],'completed')

    def test_contract_schema_file_and_public_example_match_runtime(self):
        root=Path(__file__).resolve().parents[1]
        schema=json.loads((root/'docs/schemas/worker-plugin-v1.schema.json').read_text(encoding='utf8'))
        self.assertEqual(schema['$defs']['worker']['properties']['protocol']['const'],C.PROTOCOL)
        doc=(root/'docs/worker-plugin-contract.md').read_text(encoding='utf8')
        example=json.loads(doc.split('```json\n',1)[1].split('\n```',1)[0])
        C.declarations(example['workers'])
        for invalid in ({'type':'string','properties':{}},{'type':'array','items':{'type':'object','$ref':'remote'}}):
            with self.assertRaises(W.Problem):C.schema(invalid)

    def test_worker_capabilities_do_not_grant_write_or_execution(self):
        from loop.tools import Registry,Tool
        from unittest.mock import Mock
        self.setup_worker();row=self.running(self.delegate())
        with self.assertRaises(W.Problem):self.delegate(kind='code',request_key='ungranted')
        reg=Registry();write=Mock();execute=Mock()
        reg.add(Tool('edit_project_file','fixture',{},write,zone='offflow',kind='write'))
        reg.add(Tool('run_project_check','fixture',{},execute,zone='offflow',kind='exec'))
        S.adapt_registry(reg, row['childId'])
        token=set_call(dict(sessionId=row['childId'],runId=row['childRunId'],tool='edit_project_file',actionId='c'*32))
        try:
            for name in ('edit_project_file','run_project_check'):
                with self.assertRaises(W.Problem) as caught:reg.get(name).fn()
                self.assertEqual(caught.exception.code,'worker_capability')
        finally:reset_call(token)
        write.assert_not_called();execute.assert_not_called()

    def test_scope_grants_and_other_task_are_not_inherited(self):
        self.setup_worker();row=self.running(self.delegate());result=self.publish(row)
        W.atomic_json(self.d/'session.json',dict(taskInput=dict(id='unrelated',revision=1),msgs=[]))
        with self.assertRaises(W.Problem):self.manage(row,'result',result_id=result['result_id'])
        W.atomic_json(self.d/'session.json',dict(taskInput=dict(id=self.task,revision=2),msgs=[]))
        self.assertEqual(self.manage(row,'result',result_id=result['result_id'])['data']['note'],'RESULT_PRIVATE')
        original=self.run;self.run='new-parent-run'
        W.atomic_json(self.d/'runtime.json',dict(W.RT.read(self.d),run_id=self.run))
        with self.assertRaises(W.Problem):self.manage(row,'message',request_key='next-run',text='not supported')
        self.run=original

    def test_no_plugin_default_tools_and_method_are_unchanged(self):
        self.setup_worker()
        plugins.change(self.plugin['id'],dict(expectedRevision=self.plugin['revision'],workersEnabled=False))
        session=Session(self.d);session.run_id=self.run;session.task_input={'id':self.task,'revision':2,'kind':'new'}
        root=Path(W.workspace(projects.conversation(self.parent)['workspaceId'])['path'])
        entry=NativeEntry(session,root,lambda:False);reg=default_registry(repo_root=root)
        entry.prepare(reg,'BASE','BASE',session.task_input,[],'GOAL',[],None)
        self.assertFalse(getattr(session,'worker_contract',False));self.assertEqual(P.method(session),'')
        self.assertFalse(any('worker' in name for name in reg.names()));entry.close()

    def test_plugin_results_do_not_block_legacy_join_or_default_final(self):
        self.setup_worker();row=self.delegate()
        session=SimpleNamespace(dir=self.d,run_id=self.run,emit=lambda *a,**kw:None)
        self.assertIsNone(S.join(session,timeout=.01))
        session.worker_contract=True;P.updates(session,[],closing=True)
        self.assertEqual(S.load(self.parent,row['id'])['state'],'cancelled')

    def test_child_contract_method_and_only_scoped_result_tool(self):
        self.setup_worker();row=self.running(self.delegate())
        d=self.sv._resolve_sid(row['childId']);session=Session(d);session.run_id=row['childRunId'];session.task_input={'id':'child-task','revision':1,'kind':'new'}
        root=Path(W.workspace(projects.conversation(row['childId'])['workspaceId'])['path'])
        entry=NativeEntry(session,root,lambda:False,delegated=True);reg=default_registry(repo_root=root)
        _,fragments,_,_=entry.prepare(reg,'BASE','BASE',session.task_input,[],'GOAL',[],None)
        names={x['name'] for x in reg.schemas()}
        self.assertIn('submit_worker_result',names);self.assertNotIn('delegate_plugin_worker',names)
        self.assertIn('METHOD_PRIVATE',P.method(session));entry.close()


if __name__=='__main__':unittest.main()
