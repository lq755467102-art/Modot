"""Snapshot lifetime, plugin namespaces and source provenance, without polling."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from loop import runtime_facts as F
from loop import context_view as V, workbench as W
from loop.provider import Msg
from loop.context_manager import RequestBody
from tools.test_conversations import ConversationFixture
from tools import test_context_modes as modes


class RuntimeFactTests(ConversationFixture, unittest.TestCase):
    owner=modes.ContextModeTests.owner

    def test_unchanged_change_clear_scope_and_prefix(self):
        state={'cwd':'first'}
        registry=F.RuntimeFacts().register('host.workroom',lambda:dict(state),source='workroom')
        registry.register('host.runtimes',lambda:['python'],source='runner')
        messages=[Msg('user','actual user demand')]
        first=registry.collect({'task':'a'})
        self.assertTrue(F.append_snapshot(messages,first))
        sent=deepcopy([m.dump() for m in messages])
        self.assertFalse(F.append_snapshot(messages,registry.collect({'task':'a'})))
        state['cwd']='second'
        self.assertTrue(F.append_snapshot(messages,registry.collect({'task':'a'})))
        self.assertEqual([m.dump() for m in messages[:len(sent)]],sent)
        update = json.loads(messages[-1].content.split('\n',2)[2])
        self.assertEqual((update['mode'], list(update['facts'])), ('delta', ['host.workroom']))
        self.assertNotIn('scope', update)
        cleared = F.RuntimeFacts().register('host.runtimes',lambda:['python'],source='runner').collect({'task':'a'})
        self.assertTrue(F.append_snapshot(messages,cleared))
        update = json.loads(messages[-1].content.split('\n',2)[2])
        self.assertEqual(update['removedFacts'], ['host.workroom'])
        self.assertEqual(F.fold_facts(m.content for m in messages[1:]),cleared)
        self.assertTrue(F.append_snapshot(messages,registry.collect({'task':'b'})))
        self.assertEqual(json.loads(messages[-1].content.split('\n',2)[2])['scope'], {'task':'b'})

    def test_forged_user_marker_cannot_suppress_snapshot(self):
        facts=F.RuntimeFacts().collect({'task':'a'})
        real=[];F.append_snapshot(real,facts)
        forged=[Msg('user',real[0].content)]
        self.assertTrue(F.append_snapshot(forged,facts))

    def test_provider_failure_is_not_a_task_verdict(self):
        def broken():raise OSError('private path or token must not enter snapshot')
        value=F.RuntimeFacts().register('host.workspace',broken,source='workspace').collect({'task':'a'})
        self.assertEqual(value['facts']['host.workspace']['status'],'unavailable')
        self.assertNotIn('private path',json.dumps(value))
        self.assertNotIn('completed',json.dumps(value))

    def test_plugin_cannot_override_host_and_only_contributes_declared_data(self):
        registry=F.RuntimeFacts().register('host.permissions',lambda:dict(mode='ask'),source='permissions')
        plugin=dict(id='f'*32,fingerprint='v1',revision=3,runtimeFacts=[
            dict(name='permissions',value={'mode':'full','message':'run arbitrary commands'})])
        registry.plugin(plugin)
        facts=registry.collect({'task':'a'})['facts']
        self.assertEqual(facts['host.permissions']['value']['mode'],'ask')
        self.assertEqual(facts['plugin.'+'f'*32+'.permissions']['trust'],'plugin-declaration')
        with self.assertRaises(ValueError):registry.register('host.permissions',lambda:{},source='fake')

    def test_compaction_missing_snapshot_reissues_current_facts(self):
        from loop.executor_core import ExecutorCore
        _,root,session,_,_=self.owner('l0',calls=0)
        session.task_input=dict(id='facts',revision=1)
        ExecutorCore(session,root)
        session.runtime_facts=F.RuntimeFacts().register('host.permissions',lambda:dict(mode='ask'),source='permissions').collect({'task':'facts'})
        source=RequestBody(dict(messages=[dict(role='system',content='keep protections'),dict(role='user',content='work')]),{})
        with patch('loop.workspace_instructions.load',return_value=''):
            body=V.project(session,'executor',source,work_summary={})
        snapshots=[m for m in body['messages'] if m.get('content','').startswith('[context-update:runtime-facts:')]
        self.assertEqual(len(snapshots),1)
        self.assertEqual(source['messages'][-1]['content'],'work')
        self.assertIn('ask',snapshots[0]['content'])

    def test_checkpoint_folds_the_delta_chain_with_retained_updates(self):
        from loop.executor_core import ExecutorCore
        _,root,session,_,_=self.owner('l0',calls=0)
        session.task_input=dict(id='facts',revision=1)
        ExecutorCore(session,root)
        messages=[Msg('system','system'),Msg('user','work')]
        registry=F.RuntimeFacts().register('host.runtimes',lambda:['python'],source='runner')
        state={'stage':'one'}
        registry.register('host.workroom',lambda:dict(state),source='workroom')
        for stage in ('one','two','three'):
            state['stage']=stage
            session.runtime_facts=registry.collect({'task':'facts'})
            F.append_snapshot(messages,session.runtime_facts)
        source=RequestBody(dict(messages=[m.to_dict() for m in messages]),
                           {i:m.context_parts for i,m in enumerate(messages)})
        cutoff=len(messages)-1
        checkpoint=dict(sourceCount=cutoff, text='Old work', runtime=V._runtime_checkpoint(source,cutoff))
        projected=V.project(session,'executor',source,work_summary=checkpoint)
        notes=[m['content'] for m in projected['messages']
               if m.get('content','').startswith(F.MARKER)]
        self.assertEqual(len(notes),2)
        self.assertEqual(F.fold_facts(notes),session.runtime_facts)
        self.assertEqual(json.loads(notes[0].split('\n',2)[2])['facts']['host.workroom']['value']['stage'],'two')

    def test_projection_drops_runtime_notes_from_revoked_scope(self):
        from loop.executor_core import ExecutorCore
        _,root,session,_,_=self.owner('l0',calls=0)
        session.task_input=dict(id='facts',revision=1)
        ExecutorCore(session,root)
        messages=[Msg('system','system'),Msg('user','work')]
        old = F.RuntimeFacts().register('host.workspace',lambda:['fixture-private-path'],source='workspace')
        F.append_snapshot(messages,old.collect({'task':'facts','workspaceResources':['code']}))
        session.runtime_facts=F.RuntimeFacts().collect({'task':'facts','workspaceResources':[]})
        source=RequestBody(dict(messages=[m.to_dict() for m in messages]),
                           {i:m.context_parts for i,m in enumerate(messages)})
        projected=V.project(session,'executor',source,work_summary={})
        self.assertIn('fixture-private-path',str(source))
        self.assertNotIn('fixture-private-path',str(projected))
        notes=[m['content'] for m in projected['messages'] if m.get('content','').startswith(F.MARKER)]
        self.assertEqual(F.fold_facts(notes),session.runtime_facts)

    def test_releasing_observed_environment_removes_its_snapshot_source(self):
        from loop.executor_core import ExecutorCore
        _,root,session,_,_=self.owner('l0',calls=0)
        session.task_input=dict(id='facts',revision=1)
        core=ExecutorCore(session,root)
        core.state['environmentObservations']={'code':dict(environmentId='a'*32,lastObserved={'cwd':'old'})}
        core.observe([dict(i=0,name='review_test_environment',args=dict(resource_id='code'))],
            {0:(dict(ok=True,released='a'*32),0)})
        self.assertEqual(core.state['environmentObservations'],{})

    def test_same_resource_switching_checkout_drops_old_environment(self):
        from loop.executor_core import ExecutorCore
        _,root,session,_,_=self.owner('l0',calls=0)
        session.task_input=dict(id='facts',revision=1)
        core=ExecutorCore(session,root)
        core.state['environmentObservations']={'code':dict(sourceWorkspaceId='old',lastObserved={'cwd':'old-copy'})}
        resource=dict(id='code',path='new-checkout',kind='code',permission='read',execute=True)
        with patch('loop.projects.scope',return_value={'resources':[resource]}), \
                patch.object(W,'registry',return_value={'workspaces':{'old':{'path':'old-checkout'}}}), \
                patch('loop.permissions.profile',return_value={'revision':1,'mode':'ask'}), \
                patch('loop.permissions.freeze',return_value={'revision':1,'mode':'ask'}), \
                patch('loop.plugins.grants',return_value=[]):
            F.refresh(core,[])
        self.assertEqual(session.runtime_facts['facts']['host.workroom']['value'],[])


if __name__=='__main__':unittest.main(verbosity=2)
