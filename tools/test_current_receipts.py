"""Current-run projections retain meaningful pages and keep canonical receipts immutable."""
from copy import deepcopy
import json
import unittest
import uuid

from loop import current_receipts
from tools import test_agent_code as fixtures
from tools.test_conversations import ConversationFixture


class CurrentReceiptsTests(ConversationFixture, unittest.TestCase):
    grant = fixtures.AgentCodeTests.grant

    def setUp(self):
        super().setUp()
        fixtures.AgentCodeTests.setup_code(self)
        self.session.run_id = uuid.uuid4().hex

    def project(self, items, *, reuse=True):
        messages = []
        for i, (tool, value) in enumerate(items):
            call_id = f'call-{i}'
            ref = dict(actionId=f'action-{i}', runId=self.session.run_id)
            fn = dict(name=tool, arguments=json.dumps(dict(resource_id=self.r['id'])))
            messages.append(dict(role='assistant', tool_calls=[dict(id=call_id, function=fn)]))
            messages.append(dict(role='tool', tool_call_id=call_id,
                                 content=json.dumps(dict(value, _harnessEvidence=ref), ensure_ascii=False)))
        body = dict(messages=messages)
        saved = deepcopy(body)
        view = current_receipts.for_request(self.session, body, reuse=reuse)
        self.assertEqual(body, saved)
        self.assertEqual([m['tool_call_id'] for m in view['messages'] if m['role']=='tool'],
                         [f'call-{i}' for i in range(len(items))])
        return [json.loads(m['content']) for m in view['messages'] if m['role']=='tool']

    def test_complete_directory_and_unfinished_pages(self):
        tool = 'read_project_resource'
        rows = [dict(name=f'file-{i}', path=f'src/file-{i}', directory=i % 2 == 0) for i in range(30)]
        listing = dict(ok=True, path='src', entries=rows, listingRevision='r1')
        items = [(tool, listing)]
        first = self.project(items)[0]
        self.assertEqual(first['names'], [r['name'] + ('/' if r['directory'] else '') for r in rows])
        projected = self.project(items + [(tool, dict(listing, hasMore=True)),
                                          (tool, dict(listing, listingRevision='r2')), (tool, listing)])
        self.assertEqual(first, projected[0])
        self.assertEqual((projected[0]['entryCount'], projected[0]['listingRevision']), (30, 'r1'))
        self.assertNotIn('entries', projected[0])
        self.assertEqual(projected[1]['entries'], rows)
        self.assertEqual(projected[2]['listingRevision'], 'r2')
        self.assertEqual(projected[2]['names'], first['names'])
        self.assertEqual(projected[3]['sameListingAs']['actionId'], 'action-0')
        self.grant(permission='none', write=False, execute=False, confirm=True)
        self.assertEqual(self.project(items)[0]['state'], 'scope_changed')

    def test_failure_and_distinct_pages_survive_duplicate_terminal_receipts(self):
        tool = 'read_check_output'
        sample = dict(operationId='op', receiptVersion='rev1', ok=True, text='CHECK FAILED' + ' output' * 100,
                      execution=dict(state='completed', executed=True, exitCode=0),
                      diagnostics=dict(testReport=dict(state='not_reported')), summary='检查判定 passed')
        pending = dict(operationId='op', execution=dict(state='starting'),
                       summary='preparing ' * 40)
        items = [(tool, v) for v in (pending, sample, dict(sample, receiptVersion='rev0'),
                                     dict(sample, after=1), dict(sample, text='another page'), sample)]
        view = self.project(items)
        self.assertEqual(self.project(items[:2]), view[:2])
        self.assertEqual(view[0]['execution']['state'], 'starting')
        self.assertEqual(view[-1]['sameCheckFacts']['actionId'], 'action-1')
        self.assertEqual([v['text'] for v in view[1:-1]], [items[i][1]['text'] for i in (1, 2, 3, 4)])
        self.assertEqual(view[1]['summary'], '命令退出码 0。未报告测试用例。')
        summary_source = self.project(items, reuse=False)
        self.assertEqual(summary_source[1]['summary'], view[1]['summary'])
        self.assertEqual(summary_source[1]['text'], sample['text'])
