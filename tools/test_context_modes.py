"""Real executor/scoped-role loops with local HTTP response replay and durable state."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from tools.test_conversations import ConversationFixture
from loop import execution_policy as E,context_manager as C,workbench as W,runtime as RT,delegations as D
from loop.provider import OpenAICompatProvider,Msg
from loop.session import Session
from loop.tools import Registry,Tool
from loop.loop import run
from loop import scoped_roles as R
from tools.test_context_checkpoints import fixture_input_tokens


class ContextModeTests(ConversationFixture,unittest.TestCase):
    def owner(self,crew,calls=40):
        _,thread=self.call('POST');ident=thread['id']
        root=Path(W.workspace(W.registry()['threads'][ident]['workspaceId'])['path'])
        directory=self.root/ident;directory.mkdir(exist_ok=True)
        RT.register(directory,os.getpid());session=Session(directory)
        config=dict(E.defaults(),crew=crew,modelCalls=calls,maxTurns=40)
        job=None
        if crew=='l4':
            E.configure(ident,dict(expectedRevision=1,modelCalls=calls,maxTurns=40))
            spec=dict(goal='Retain original goal',boundaries='No additional grants',
                criteria=[dict(id='human',kind='manual',text='Human must review output')],maxRounds=3,modelCalls=80)
            job=D.create(ident,spec)
            job=D.lock(ident,job['id'],dict(version=job['versions'][-1]['id'],confirm=True,acknowledgeWeakChecks=True,expectedRevision=job['revision']))
            config['delegationId']=job['id']
        E.begin(session,config,root)
        p=OpenAICompatProvider(base_url='http://127.0.0.1:1/v1',api_key='local-only',model='replay');p.max_tokens=1024
        C.bind_capacity(session,p,50000)
        p.count_input_tokens=fixture_input_tokens
        return ident,root,session,p,job

    def replay(self,count):
        seen=[]
        def once(request,should_stop=None):
            body=json.loads(request.data);seen.append(body)
            C.validate_pairs(body['messages'])
            self.assertLess(fixture_input_tokens(body),50000)
            n=len(seen)
            message=dict(role='assistant',content='Local public action '+str(n))
            if n<=count:message['tool_calls']=[dict(id='turn-'+str(n),type='function',function=dict(name='observe',arguments=json.dumps(dict(step=n,material='x'*12000))))]
            return dict(choices=[dict(message=message,finish_reason='tool_calls' if n<=count else 'stop')],usage={})
        return seen,once

    def registry(self,effects):
        def observe(step,material):
            effects.append(step);return dict(ok=True,text='Observed fact '+str(step)+'\n'+'r'*9000)
        return Registry().add(Tool('observe','Read local fixture',{'type':'object','properties':{'step':{'type':'integer'},'material':{'type':'string'}}},observe,zone='offflow',readonly=True,kind='read'))

    def test_native_l0_and_delegation_retain_budget_and_goal_after_compactions(self):
        for crew in ('l0','l4'):
            with self.subTest(crew=crew):
                ident,root,s,p,job=self.owner(crew);effects=[];seen,once=self.replay(26)
                with patch.object(p,'_once',side_effect=once):
                    result=run('Original goal; preserve files; unfinished requirement must stay visible',E.provider(p,s,'executor'),self.registry(effects),lambda:dict(ok=True),
                        max_turns=30,max_same=100,session=s,parallel_tools=False,system='No extra authorization')
                self.assertEqual(result['status'],'success')
                self.assertEqual(effects,list(range(1,27)))
                checkpoints=C.checkpoint_page(s.dir)
                self.assertGreaterEqual(checkpoints['totalCheckpoints'],3)
                state=s.load();self.assertTrue(any('unfinished requirement' in str(m['content']) for m in state['msgs']))
                self.assertTrue(any('historicalContext' in str(m['content']) for m in state['msgs']))
                budget=W.read(s.execution_budget)
                self.assertEqual(budget['usedCalls'],27);self.assertEqual(budget['byRole'],{'executor':27})
                self.assertEqual(budget['config']['crew'],crew)
                self.assertEqual(W.registry()['threads'][ident].get('resourceIds',[]),[])
                # Restart reads the adopted working list; it does not re-run any historical tool.
                restarted=Session(s.dir)
                restarted.execution_budget=s.execution_budget
                restarted.context_model_limits=dict(s.context_model_limits)
                before=list(effects)
                with patch.object(p,'_once',return_value=dict(choices=[dict(message=dict(content='Continue from retained records'),finish_reason='stop')])):
                    run('resume',E.provider(p,restarted,'executor'),self.registry(effects),lambda:dict(ok=True),session=restarted,
                        resume_msgs=[Msg.load(m) for m in state['msgs']],max_turns=1)
                self.assertEqual(effects,before)
                self.assertEqual(W.read(s.execution_budget)['usedCalls'],28)
                if job:
                    fresh=D.load(ident,job['id']);self.assertEqual(fresh['activeVersion'],job['activeVersion']);self.assertIsNone(fresh.get('decision'))

    def test_planner_and_reviewer_adopt_their_own_lists_and_share_existing_calls(self):
        for role in ('planner','reviewer'):
            with self.subTest(role=role):
                _,root,s,p,_=self.owner('l4');effects=[];seen,once=self.replay(5)
                # Large initial material exercises the short, bounded role loop too.
                with patch.object(p,'_once',side_effect=once),patch.object(p,'supports_stream',False),patch.object(R,'registry',return_value=self.registry(effects)):
                    result=R.run(root,s,role,'Retain scoped review facts.\n'+'g'*43000,p, readonly_review=role=='reviewer')
                self.assertIn('Local public action',result)
                self.assertGreaterEqual(C.checkpoint_page(s.dir)['totalCheckpoints'],2)
                self.assertEqual(effects,[1,2,3,4,5])
                self.assertEqual(W.read(s.execution_budget)['byRole'],{role:6})
                self.assertTrue(all(r['role']==role for r in C.checkpoint_page(s.dir)['checkpoints']))


if __name__=='__main__':unittest.main()
