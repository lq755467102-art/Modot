"""Native tool exposure/skill seams; no model network or command execution."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from loop.tools import Tool, default_registry
from loop.executor_core import ExecutorCore
from tools import test_l0_contract_notes as notes

CORE = {'ask_human','read_project_resource','read_image','inspect_code','search_project_code',
        'read_check_output','edit_project_file','write_project_file','run_project_check',
        'run_project_shell','inspect_git_repository','open_workroom_file',
        'record_working_summary','read_task_requirements','use_tool_group'}


def exposed(registry):
    return {t['name']for t in registry.schemas()}


class NativeToolsFixture(unittest.TestCase):
    def setUp(self):
        self.fixture=notes.ContractNotes();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.session=self.fixture.session;self.root=self.fixture.execution_root

    def registry(self, extra=None):
        reg=default_registry(self.root)
        if extra:reg.add(extra)
        core=ExecutorCore(self.session,self.root);core.install(reg)
        return core,reg


class NativeCoreTools(NativeToolsFixture):
    def test_archived_thread_exposes_only_the_history_lookup_before_group_selection(self):
        self.session.task_history_start = 4
        _, registry = self.registry()
        self.assertEqual(exposed(registry), CORE | {'search_conversation_history'})
        registry.get('use_tool_group')({'group': 'history'})
        self.assertIn('read_action_evidence', exposed(registry))

    def test_unclassified_registration_does_not_enter_model_tools(self):
        _,reg=self.registry(Tool('unclassified_fixture','not a default capability',
            {'type':'object','properties':{}},lambda:dict(ok=True),zone='offflow',kind='read',readonly=True))
        self.assertEqual(exposed(reg),CORE)
        self.assertNotIn('unclassified_fixture',exposed(reg))

    def test_optional_group_selection_survives_reinstall(self):
        core,reg=self.registry()
        self.assertNotIn('read_check_queue',exposed(reg))
        reg.get('use_tool_group')({'group':'verification'})
        self.assertIn('read_check_queue',exposed(reg))
        again,reg2=self.registry()
        self.assertIn('read_check_queue',exposed(reg2))
        self.assertIn('verification',again.state['toolGroups'])

    def test_explicit_required_evidence_keeps_verification_available(self):
        with patch('loop.delivery_review.load',return_value=dict(requirements=[
                dict(id='user-required',required=True,logicalTaskId='contracts-task')])):
            _,reg=self.registry()
        self.assertIn('inspect_delivery',exposed(reg))
        self.assertIn('record_delivery_evidence',exposed(reg))


class NativeLocalSkills(NativeToolsFixture):
    def setUp(self):
        super().setUp()
        self.skills=self.fixture.root/'test-skills';self.skills.mkdir()
        self.env=patch.dict(os.environ,HARNESS_SKILLS=str(self.skills));self.env.start();self.addCleanup(self.env.stop)
        self.session.context_preferences=dict(effective=dict(skills=True))

    def skill(self,name,body='METHOD',meta=''):
        folder=self.skills/name;folder.mkdir(exist_ok=True)
        (folder/'SKILL.md').write_text('---\nname: '+name+'\ndescription: test method\n'+meta+'---\n'+body,'utf8')
        return folder

    def catalog(self):
        from loop import skills
        self.session.context_sources=[dict(kind='skills',text=skills.prompt_block(self.root))]

    def test_catalog_has_a_callable_read_entry(self):
        self.skill('example');self.catalog();_,reg=self.registry()
        self.assertIn('load_skill',exposed(reg))
        self.assertEqual(reg.get('load_skill')({'name':'example'})['body'],'METHOD')

    def test_empty_or_disabled_catalog_has_no_extra_tool(self):
        self.catalog();_,reg=self.registry();self.assertNotIn('load_skill',exposed(reg))
        self.skill('example');self.catalog();self.session.context_preferences['effective']['skills']=False
        _,reg=self.registry();self.assertNotIn('load_skill',exposed(reg))

    def test_pages_rejoin_and_invocation_flags_are_checked_at_read(self):
        text='BEGIN\n'+('reference facts\n'*1700)+'END'
        folder=self.skill('example',text);self.skill('manual','PRIVATE','disable-model-invocation: true\n')
        self.catalog();_,reg=self.registry();tool=reg.get('load_skill');self.assertIsNotNone(tool)
        first=tool({'name':'example'});second=tool({'name':'example','offset':first['nextOffset'],'source_token':first['source_token']})
        self.assertEqual(first['body']+second['body'],text)
        self.assertFalse(tool({'name':'manual'})['ok'])
        (folder/'SKILL.md').write_text('---\nname: example\ndisable-model-invocation: true\n---\nREVOKED','utf8')
        self.assertFalse(tool({'name':'example'})['ok'])
        self.assertNotIn('REVOKED',json.dumps(tool({'name':'example'})))

    def test_new_names_not_in_this_run_catalog_are_not_readable(self):
        self.skill('known');self.catalog();_,reg=self.registry();tool=reg.get('load_skill');self.assertIsNotNone(tool)
        self.skill('added-later','NOT_ADVERTISED')
        value=tool({'name':'added-later'});self.assertFalse(value['ok'])
        self.assertNotIn('NOT_ADVERTISED',json.dumps(value))

    def test_deleted_skill_fails_without_body_and_user_flag_stays_independent(self):
        from loop import skills
        folder=self.skill('known');self.skill('automatic-only','AUTO','user-invocable: false\n')
        self.catalog();_,reg=self.registry();tool=reg.get('load_skill');self.assertIsNotNone(tool)
        self.assertEqual(tool({'name':'automatic-only'})['body'],'AUTO')
        self.assertFalse(skills.load('automatic-only',self.root,model=False)['ok'])
        (folder/'SKILL.md').unlink()
        value=tool({'name':'known'});self.assertFalse(value['ok']);self.assertNotIn('body',value)


if __name__=='__main__':unittest.main(verbosity=2)
