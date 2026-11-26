"""Default memory through the same service/tools/request seams used by L0."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import test_memory_runtime as fixtures, test_memory_trees as treefixtures
from loop import memory_service as M, memory_learning as L, habits as H
from loop.policy import Policy
from loop.tools import set_call, reset_call


class DefaultMemory(treefixtures.Trees):
    owner = fixtures.Runtime.owner
    def setUp(self):
        super().setUp()
        p = patch('loop.charter.load_config', return_value=dict(memory_tiers='habits=propose,rules=propose,facts=edit', roots=[]))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(H, 'index_path', return_value=self.root / 'owned-habits.json')
        p.start()
        self.addCleanup(p.stop)

    def test_audit_does_not_change_selection_identity(self):
        fact = self.add(self.a)
        before = self.view(self.a)
        L.record_injection(self.a, 'audit-run', [dict(id=fact['id'], version=1)])
        after = self.view(self.a)
        self.assertGreater(after['revision'], before['revision'])
        self.assertEqual(after['selectionVersion'], before['selectionVersion'])
        self.assertEqual(after['nodes'][0]['weight'], before['nodes'][0]['weight'])
        M.mutate(self.b, dict(operation='mode', mode='lean', expectedRevision=after['revision']))
        self.assertEqual(self.view(self.a)['selectionVersion'], after['selectionVersion'])

    def test_readonly_discovery_does_not_grant_source_writes(self):
        policy = Policy('readonly', root=self.root, rules={})
        self.assertTrue(policy.check('use_tool_group', {'group': 'memory'}, 'read')[0])
        self.assertFalse(policy.check('write_file', {'path': str(self.root/'source.py')}, 'write')[0])
        from loop.executor_core import ExecutorCore
        from loop.tools import default_registry
        session, _ = self.owner(self.a)
        session.run_id = None
        root = Path(M.W.workspace(M.projects.conversation(self.a)['workspaceId'])['path'])
        core = ExecutorCore(session, root)
        reg = default_registry(root, mem=dict(facts='edit', rules='propose'))
        core.install(reg)
        self.assertEqual(reg.get('use_tool_group').kind, 'read')

    def test_budget_node_can_be_fetched_without_bypassing_off(self):
        fact = self.add(self.a, body='x'*8000)
        self.add(self.a, title='Second', body='y'*8000)
        self.add(self.a, title='Third', body='z'*8000)
        self.assertFalse(next(n for n in self.view(self.a)['nodes'] if n['id']==fact['id'])['selected'])
        detail = M.model_page(self.a, node_id=fact['id'], version=1, tiers=dict(facts='read'))
        self.assertEqual(detail['nodes'][0]['body'], 'x'*8000)
        M.mutate(self.a, dict(operation='mode', mode='off', expectedRevision=self.view(self.a)['revision']))
        with self.assertRaises(M.W.Problem):
            M.model_page(self.a, node_id=fact['id'], tiers=dict(facts='read'))

    def collect(self, ident=None, **kw):
        ident = ident or self.a
        store = M.target(ident, scope='session')[0]
        return M.mutate(ident, dict(dict(expectedRevision=store.load()['revision'],
            title='Collected observation', body='A reusable fact'), **kw))

    def event(self, node_id, kind, ident=None, **kw):
        ident = ident or self.a
        store = M.node_target(ident, node_id)[0]
        return M.mutate(ident, dict(operation='evidence', id=node_id, kind=kind,
            expectedRevision=store.load()['revision'], **kw))

    def check(self, ident, key):
        from loop import code_service as C
        project = M.projects.conversation(ident)['projectId']
        directory = self.root / ('code-' + ident)
        directory.mkdir(exist_ok=True)
        resource = M.W.handle('POST', '/api/v2/projects/'+project+'/resources', {},
            dict(path=str(directory), kind='code'), self.sv)[1]
        def grant(row):
            row['projects'][project]['resources'][resource['id']].update(permission='read')
            row['threads'][ident].update(resourceIds=[resource['id']])
        M.W.change_registry(grant)
        C.record(ident, dict(id=key, operation='check', resourceId=resource['id'],
            source=dict(runId='run-'+ident), state='completed',
            result=dict(process=dict(state='completed', exitCode=0))))
        return key

    def use(self, node_id, used_by, operation_id, ident=None):
        return self.event(node_id, 'applied', ident, conversationId=used_by,
            operationId=operation_id, note='This task independently exercised the recorded behavior')

    def promote(self, node_id, ident=None):
        ident = ident or self.a
        store = M.node_target(ident, node_id)[0]
        return M.mutate(ident, dict(operation='promote', id=node_id,
            expectedRevision=store.load()['revision']))

    def adopt(self, proposal):
        return M.mutate(self.a, dict(operation='adopt', proposalId=proposal['proposal']['id']))

    def test_model_collection_is_private_and_duplicate_is_idempotent(self):
        session, _ = self.owner(self.a)
        root = Path(M.W.workspace(M.projects.conversation(self.a)['workspaceId'])['path'])
        token = set_call(dict(sessionId=self.a, runId=session.run_id, actionId='collect-action'))
        try:
            saved = M.tool_remember(root, dict(facts='edit'), 'Only the collecting conversation may see this')
            duplicate = M.tool_remember(root, dict(facts='edit'), 'Only the collecting conversation may see this')
        finally:
            reset_call(token)
        self.assertEqual(saved['scope'], 'session')
        self.assertEqual(saved['id'], duplicate['id'])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(self.view(self.b)['nodes'], [])
        node = self.view(self.a)['nodes'][0]
        self.assertEqual(node['sources'][0]['actionId'], 'collect-action')
        self.assertEqual(node['version'], 1)

    def test_project_promotion_requires_independent_use_and_preserves_weight(self):
        node = self.collect(weight=37)
        check_a = self.check(self.a, 'first-check')
        check_b = self.check(self.b, 'second-check')
        self.event(node['id'], 'human-confirmed')
        self.use(node['id'], self.a, check_a)
        for _ in range(3):
            self.use(node['id'], self.a, check_a)
        self.assertFalse(self.promote(node['id'])['qualification']['eligible'])
        self.use(node['id'], self.b, check_b)
        proposal = self.promote(node['id'])
        self.assertTrue(proposal['qualification']['eligible'])
        self.assertEqual(self.view(self.b)['nodes'], [])
        adopted = self.adopt(proposal)
        visible = self.view(self.b)['nodes']
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]['id'], adopted['id'])
        self.assertEqual(visible[0]['weight'], 37)
        self.assertEqual(visible[0]['scope'], 'project')
        self.assertEqual(self.view(self.other)['nodes'], [])
        self.assertEqual(len([n for n in self.view(self.a)['nodes'] if n['selected']]), 1)
        self.assertEqual(len([e for e in visible[0]['evidence'] if e['kind']=='applied']), 2)

    def prepared_project(self):
        node = self.collect(weight=37, entryType='rule')
        self.use(node['id'], self.a, self.check(self.a, 'prepare-a'))
        self.use(node['id'], self.b, self.check(self.b, 'prepare-b'))
        self.event(node['id'], 'human-confirmed')
        self.event(node['id'], 'challenge', attempt='Consider a second implementation with a different caller', outcome='not_found')
        return self.adopt(self.promote(node['id']))

    def test_cross_project_habit_needs_second_project_and_counterexample_attempt(self):
        project = self.prepared_project()
        self.assertFalse(self.promote(project['id'])['qualification']['eligible'])
        other_project = M.W.handle('POST', '/api/v2/projects', {}, dict(name='second independent project'), self.sv)[1]
        other = self.call('POST', body=dict(projectId=other_project['id']))[1]['id']
        self.use(project['id'], other, self.check(other, 'other-check'))
        proposal = self.promote(project['id'])
        self.assertTrue(proposal['qualification']['eligible'])
        self.assertFalse(H.path().exists())
        adopted = self.adopt(proposal)
        self.assertTrue(adopted['ok'])
        habit = next(n for n in self.view(other)['nodes'] if n['id']==adopted['id'])
        self.assertEqual(habit['body'], 'A reusable fact')
        self.assertEqual(habit['weight'], 37)
        self.assertEqual(habit['scope'], 'habit')
        self.assertTrue(all(s.get('projectId')==other_project['id'] for s in habit['sources']))
        self.assertNotIn('"body":', json.dumps(H.load_index()['memory']))
        self.assertEqual(H.path().read_text(encoding='utf8').count('A reusable fact'), 1)

    def test_unreviewed_rule_and_unresolved_counterexample_cannot_promote(self):
        node = self.collect(entryType='rule')
        self.use(node['id'], self.a, self.check(self.a, 'rule-a'))
        self.use(node['id'], self.b, self.check(self.b, 'rule-b'))
        self.event(node['id'], 'human-confirmed')
        self.assertIn('反例', str(self.promote(node['id'])['qualification']['reasons']))
        self.event(node['id'], 'challenge', attempt='An empty input with a second caller', outcome='found')
        self.event(node['id'], 'challenge', attempt='Another concrete caller', outcome='not_found')
        self.assertFalse(self.promote(node['id'])['qualification']['eligible'])
        self.event(node['id'], 'resolve-counterexample')
        self.assertTrue(self.promote(node['id'])['qualification']['eligible'])

    def test_changed_version_and_revoked_use_invalidate_proposal(self):
        node = self.collect()
        self.use(node['id'], self.a, self.check(self.a, 'stale-a'))
        self.use(node['id'], self.b, self.check(self.b, 'stale-b'))
        self.event(node['id'], 'human-confirmed')
        proposal = self.promote(node['id'])
        store = M.node_target(self.a, node['id'])[0]
        M.mutate(self.a, dict(id=node['id'], expectedRevision=store.load()['revision'],
            title='Changed', body='A materially changed observation'))
        self.assertFalse(self.promote(node['id'])['qualification']['eligible'])
        with self.assertRaises(M.W.Problem):
            self.adopt(proposal)

    def test_model_cannot_confirm_resolve_or_claim_another_conversations_use(self):
        node = self.collect()
        check = self.check(self.b, 'forged-use')
        for kind, fields in [('human-confirmed', {}), ('resolve-counterexample', {}),
                ('applied', dict(conversationId=self.b, operationId=check, note='forged'))]:
            with self.subTest(kind=kind), self.assertRaises(M.W.Problem):
                M.mutate(self.a, dict(operation='evidence', id=node['id'],
                    expectedRevision=M.node_target(self.a, node['id'])[0].load()['revision'], kind=kind, **fields), 'model')

    def test_readonly_evidence_cannot_maintain_and_source_revocation_blocks_promotion(self):
        node = self.collect()
        first = self.check(self.a, 'grant-a')
        second = self.check(self.b, 'grant-b')
        self.use(node['id'], self.a, first)
        self.use(node['id'], self.b, second)
        self.event(node['id'], 'human-confirmed')
        proposal = self.promote(node['id'])
        resource = M.projects.conversation(self.b)['resourceIds'][0]
        M.W.change_registry(lambda row: row['projects'][self.p['id']]['resources'][resource].update(permission='none'))
        self.assertFalse(self.promote(node['id'])['qualification']['eligible'])
        with self.assertRaises(M.W.Problem):
            self.adopt(proposal)
        M.W.change_registry(lambda row: row['threads'][self.a].update(readonlyEvidence=True))
        from loop.memory_tools import tools
        root = Path(M.W.workspace(M.projects.conversation(self.a)['workspaceId'])['path'])
        self.assertFalse(any(t.name=='remember' for t in tools(dict(facts='edit'), root)))
        with self.assertRaises(M.W.Problem):
            self.event(node['id'], 'human-confirmed')

    def test_habit_identity_survives_rename_and_edit_does_not_reuse_old_evidence(self):
        project = self.prepared_project()
        other_project = M.W.handle('POST', '/api/v2/projects', {}, dict(name='rename proof'), self.sv)[1]
        other = self.call('POST', body=dict(projectId=other_project['id']))[1]['id']
        self.use(project['id'], other, self.check(other, 'rename-use'))
        adopted = self.adopt(self.promote(project['id']))
        old = H.memory_nodes()[0]
        self.assertTrue(H.rename(old['title'], 'Renamed habit')['ok'])
        renamed = H.memory_nodes()[0]
        self.assertEqual(renamed['id'], adopted['id'])
        self.assertGreater(renamed['version'], old['version'])
        H.edit_body('Renamed habit', 'A changed human instruction')
        self.assertEqual(H.memory_nodes()[0]['evidence'], [])
        H.remove('Renamed habit')
        H.add('Renamed habit', 'A new unrelated habit')
        self.assertEqual(H.memory_nodes(), [])

    def test_collection_version_check_survives_unrelated_audit_revision(self):
        session, _ = self.owner(self.a)
        root = Path(M.W.workspace(M.projects.conversation(self.a)['workspaceId'])['path'])
        token = set_call(dict(sessionId=self.a, runId=session.run_id, actionId='versioned-collect'))
        try:
            node = M.tool_remember(root, dict(facts='edit'), 'One observed fact')
            L.record_injection(self.a, 'second-audit', [dict(id=node['id'], version=1)])
            updated = M.tool_remember(root, dict(facts='edit'), 'Updated fact', id=node['id'], title='Updated', expectedVersion=1)
            self.assertEqual(updated['version'], 2)
            with self.assertRaises(M.W.Problem):
                M.tool_remember(root, dict(facts='edit'), 'Stale overwrite', id=node['id'], title='Stale', expectedVersion=1)
        finally:
            reset_call(token)

    def test_promoted_body_is_not_charged_twice_against_layer_budget(self):
        node = self.collect(body='x'*8000)
        self.use(node['id'], self.a, self.check(self.a, 'large-a'))
        self.use(node['id'], self.b, self.check(self.b, 'large-b'))
        self.event(node['id'], 'human-confirmed')
        adopted = self.adopt(self.promote(node['id']))
        view = self.view(self.a)
        self.assertEqual([n['id'] for n in view['nodes'] if n['selected']], [adopted['id']])
        self.assertEqual(M.model_page(self.a, node_id=node['id'])['nodes'][0]['body'], 'x'*8000)

    def test_interrupted_human_adoption_reuses_published_project_node(self):
        from loop import proposals as P
        node = self.collect()
        self.use(node['id'], self.a, self.check(self.a, 'replay-a'))
        self.use(node['id'], self.b, self.check(self.b, 'replay-b'))
        self.event(node['id'], 'human-confirmed')
        proposal = self.promote(node['id'])
        with patch.object(P, 'save', side_effect=OSError('crash before proposal removal')):
            with self.assertRaises(OSError):
                self.adopt(proposal)
        published = self.view(self.b)['nodes']
        retried = self.adopt(proposal)
        self.assertTrue(retried['duplicate'])
        self.assertEqual([n['id'] for n in published], [n['id'] for n in self.view(self.b)['nodes']])
        self.assertTrue(self.promote(node['id'])['promoted'])
        self.assertFalse(P.get(M.W.home(), proposal['proposal']['id']))

    def test_non_code_use_can_be_human_recorded_without_fabricated_check(self):
        node = self.collect()
        self.event(node['id'], 'applied', conversationId=self.a, note='User reports this document task used the observation')
        self.event(node['id'], 'applied', conversationId=self.b, note='User reports a separate document task used it')
        self.event(node['id'], 'human-confirmed')
        self.assertTrue(self.promote(node['id'])['qualification']['eligible'])
        events = M.node_target(self.a, node['id'])[0].load()['nodes'][node['id']]['evidence']
        self.assertFalse(any(e.get('operationId') or e.get('runId') for e in events))
        with self.assertRaises(M.W.Problem):
            M.mutate(self.a, dict(operation='evidence', id=node['id'], kind='applied', note='model claim',
                expectedRevision=M.node_target(self.a, node['id'])[0].load()['revision']), 'model')

    def test_project_promotion_preserves_branch_and_inference_scope(self):
        node = self.collect(branches=['feature-only'], kind='inference')
        self.event(node['id'], 'applied', conversationId=self.a, note='First independently reported use')
        self.event(node['id'], 'applied', conversationId=self.b, note='Second independently reported use')
        self.event(node['id'], 'human-confirmed')
        adopted = self.adopt(self.promote(node['id']))
        result = M.node_target(self.a, adopted['id'])[0].load()['nodes'][adopted['id']]
        self.assertEqual(result['branches'], ['feature-only'])
        self.assertEqual(result['kind'], 'inference')
        self.assertIn('限定分支', str(self.promote(adopted['id'])['qualification']['reasons']))

    def test_interrupted_habit_adoption_reuses_the_same_habits_body(self):
        from loop import proposals as P
        project = self.prepared_project()
        other_project = M.W.handle('POST', '/api/v2/projects', {}, dict(name='habit crash'), self.sv)[1]
        other = self.call('POST', body=dict(projectId=other_project['id']))[1]['id']
        self.event(project['id'], 'applied', conversationId=other, note='User independently used this rule in a second project')
        proposal = self.promote(project['id'])
        with patch.object(P, 'save', side_effect=OSError('crash after HABITS publish')):
            with self.assertRaises(OSError):
                self.adopt(proposal)
        original = H.path().read_bytes()
        old_id = H.memory_nodes()[0]['id']
        self.assertTrue(self.adopt(proposal)['duplicate'])
        self.assertEqual(H.path().read_bytes(), original)
        self.assertEqual(H.memory_nodes()[0]['id'], old_id)

    def test_distinct_host_tasks_count_but_retried_checks_do_not(self):
        from loop import code_service as C
        node = self.collect()
        operation = self.check(self.a, 'logical-first')
        original = C.operation(self.a, operation)
        # Same session, distinct host task identities; not model-supplied run names.
        for index, task_id in enumerate(['task-one', 'task-one', 'task-two']):
            op = dict(original, id='logical-'+str(index), source=dict(runId='attempt-'+str(index), logicalTaskId=task_id))
            C.record(self.a, op)
            self.use(node['id'], self.a, op['id'])
            self.event(node['id'], 'human-confirmed')
            self.assertEqual(self.promote(node['id'])['qualification']['eligible'], index==2)

    def test_habit_promotion_requires_habit_proposal_grant(self):
        from loop.memory_tools import tools
        project = self.prepared_project()
        session, _ = self.owner(self.a)
        root = Path(M.W.workspace(M.projects.conversation(self.a)['workspaceId'])['path'])
        available = tools(dict(facts='edit', rules='propose', habits='read'), root)
        promote = next(t for t in available if t.name=='propose_learned_habit')
        token = set_call(dict(sessionId=self.a, runId=session.run_id, actionId='unauthorized-habit-proposal'))
        try:
            denied = promote({'id': project['id'], 'targetScope': 'habit'})
            self.assertEqual(denied['code'], 'memory_denied')
            self.assertEqual(denied['memoryWrite']['status'], 'not_saved')
        finally:
            reset_call(token)


if __name__ == '__main__':
    unittest.main()
