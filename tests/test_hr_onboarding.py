from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from boss_hire.job_setup import run_job_read, read_selected_job
from boss_hire.local_security import atomic_write_json
from boss_hire.state_store import jd_hash
from boss_hire.supply_inventory import CandidateInventory
from boss_hire.workflow_run import workflow_paths
from scripts import run_single_job_live as launcher
from scripts.setup_hr import initialize
from tests import test_single_job_llm as llm_fixtures


class HrOnboardingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = workflow_paths(self.root)
        initialize(self.root)
        self.now = lambda: datetime(2026, 9, 9, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.job = llm_fixtures.SingleJobLlmTests().open_job()
        self.llm = llm_fixtures.FakeLlm({'rubric': llm_fixtures.rubric_result(), 'search_plan': llm_fixtures.search_semantics_result()})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(launcher, 'ROOT', self.root))
        self.stack.enter_context(patch.object(launcher, 'FIXED_AUTH_DIR', self.root / 'auth'))
        self.stack.enter_context(patch.object(launcher, 'FIXED_GUARD_DIR', self.root / 'guard'))
        self.stack.enter_context(patch.object(launcher, '_scoring_settings', return_value={'base_url': 'https://example.invalid', 'api_key': 'fake', 'model': 'fake-model', 'workers': 4}))
        self.factory = self.stack.enter_context(patch.object(launcher, 'OpenAICompatibleJsonLlm', return_value=self.llm))
        self.sync = self.stack.enter_context(patch.object(launcher, 'sync_auth_from_chrome', side_effect=AssertionError('no real auth')))

    def selected(self):
        return {'account_key': launcher.account_key_for(self.root / 'auth'), 'job_id': self.job.encrypt_job_id,
                'job': self.job.to_dict(), 'source_jd_hash': jd_hash(self.job.to_jd_text())}

    def save_selected(self):
        atomic_write_json(self.paths.work_root / 'selected_job.json', self.selected())

    def test_clean_job_selection_then_generation_start_and_reuse(self):
        kinds = []
        def read(plan, paths, current):
            kinds.append(plan['plan_kind'])
            if plan['plan_kind'] == 'job_catalog':
                return {'contract': 'job_catalog', 'account_key': plan['account_key'], 'board_date': plan['board_date'],
                        'jobs': [{'encrypt_job_id': 'unselected', 'name': self.job.name, 'online_status': 1},
                                 {'encrypt_job_id': self.job.encrypt_job_id, 'name': self.job.name, 'online_status': 1}]}
            self.assertEqual(plan['summary']['encrypt_job_id'], self.job.encrypt_job_id)
            return self.selected()
        with patch.object(launcher, '_execute_job_read_after_confirmation', side_effect=read):
            launcher.jobs_command(launcher.parse_args(['jobs', '--refresh']), input_fn=lambda _: '确认读取岗位列表1次', output_fn=lambda _: None, now=self.now)
            launcher.jobs_command(launcher.parse_args(['jobs', '--select', '2']), input_fn=lambda _: '确认读取所选JD1次', output_fn=lambda _: None, now=self.now)
        self.assertEqual(kinds, ['job_catalog', 'job_snapshot'])
        self.assertFalse(self.paths.inventory_path.exists())
        launcher.prepare_job_command(launcher.parse_args(['prepare-job']), input_fn=lambda _: '确认生成岗位材料', output_fn=lambda _: None, now=self.now)
        prepared = launcher._prepare_start(config_name='default', paths=self.paths, board_date='2026-09-09')
        self.assertEqual(prepared.plan['selected_job_id'], self.job.encrypt_job_id)
        self.assertEqual(prepared.search_plan['schema_version'], 3)
        self.factory.reset_mock()
        launcher.prepare_job_command(launcher.parse_args(['prepare-job']), input_fn=lambda _: self.fail('reuse must not ask'), output_fn=lambda _: None, now=self.now)
        self.factory.assert_not_called()
        self.sync.assert_not_called()

    def test_mismatch_confirmation_never_creates_clients(self):
        with self.assertRaisesRegex(ValueError, '尚无'):
            launcher.prepare_job_command(launcher.parse_args(['prepare-job']))
        with self.assertRaisesRegex(Exception, '确认'):
            launcher.jobs_command(launcher.parse_args(['jobs', '--refresh']), input_fn=lambda _: 'yes', output_fn=lambda _: None, now=self.now)
        self.save_selected()
        with self.assertRaisesRegex(Exception, '确认'):
            launcher.prepare_job_command(launcher.parse_args(['prepare-job']), input_fn=lambda _: 'yes', output_fn=lambda _: None)
        self.factory.assert_not_called()
        self.sync.assert_not_called()

    def test_generation_failure_leaves_no_ready_inventory(self):
        self.save_selected()
        with patch('boss_hire.single_job_llm.generate_search_plan', side_effect=ValueError('invalid response')):
            with self.assertRaisesRegex(ValueError, 'invalid response'):
                launcher.prepare_job_command(launcher.parse_args(['prepare-job']), input_fn=lambda _: '确认生成岗位材料', output_fn=lambda _: None)
        self.assertEqual(CandidateInventory.load(self.paths.inventory_path).to_dict()['job_artifacts'], {})
        launcher.prepare_job_command(launcher.parse_args(['prepare-job']), input_fn=lambda _: '确认生成岗位材料', output_fn=lambda _: None)
        self.assertIn(self.job.encrypt_job_id, CandidateInventory.load(self.paths.inventory_path).to_dict()['job_artifacts'])

    def test_old_zero_route_cache_is_regenerated_under_current_generator(self):
        self.save_selected()
        source_hash = jd_hash(self.job.to_jd_text())
        rubric = llm_fixtures.generate_continuous_rubric(
            llm_fixtures.FakeLlm({'rubric': llm_fixtures.rubric_result()}),
            self.job.to_jd_text(),
        )
        inventory = CandidateInventory()
        inventory.record_job_artifacts(
            self.job.encrypt_job_id,
            source_hash,
            rubric=rubric,
            search_plan={
                'schema_version': 3,
                'contract': 'generic_search_plan',
                'source_jd_hash': source_hash,
                'version': 'search-old-zero',
                'target_route_count': 12,
                'generation_shortfall': 12,
                'routes': [],
            },
        )
        inventory.save(self.paths.inventory_path)
        output = []

        launcher.prepare_job_command(
            launcher.parse_args(['prepare-job']),
            input_fn=lambda _: '确认生成岗位材料',
            output_fn=output.append,
        )

        stored = CandidateInventory.load(self.paths.inventory_path).get_job_artifacts(
            self.job.encrypt_job_id,
            source_hash,
        )
        self.assertEqual(
            stored['search_plan']['search_generator_version'],
            llm_fixtures.SEARCH_GENERATOR_VERSION,
        )
        self.assertTrue(stored['search_plan']['routes'])
        self.assertTrue(any('旧搜索生成契约已失效' in line for line in output))

    def test_active_run_blocks_setup_and_stale_catalog_rejects_selection(self):
        with patch('boss_hire.workflow_run._load_active_unlocked') as active:
            active.return_value.state = {'status': 'source_complete'}
            with self.assertRaisesRegex(ValueError, '未结束'):
                launcher.jobs_command(launcher.parse_args(['jobs', '--refresh']), input_fn=lambda _: self.fail('no confirmation'), output_fn=lambda _: None, now=self.now)
        atomic_write_json(self.paths.work_root / 'job_catalog.json', {'contract': 'job_catalog', 'account_key': launcher.account_key_for(self.root / 'auth'), 'board_date': '2026-09-08', 'jobs': []})
        with self.assertRaisesRegex(ValueError, '跨日'):
            launcher.jobs_command(launcher.parse_args(['jobs', '--select', '1']), now=self.now)

    def test_jd_change_rejects_start_until_new_materials_generated(self):
        self.save_selected()
        launcher.prepare_job_command(launcher.parse_args(['prepare-job']), input_fn=lambda _: '确认生成岗位材料', output_fn=lambda _: None)
        selected = self.selected()
        selected['job']['description'] += '\n新的工作职责'
        from boss_hire.recruiter_jobs import RecruiterJob
        selected['source_jd_hash'] = jd_hash(RecruiterJob(**selected['job']).to_jd_text())
        atomic_write_json(self.paths.work_root / 'selected_job.json', selected)
        with self.assertRaisesRegex(ValueError, 'JD 已变化'):
            launcher._prepare_start(config_name='default', paths=self.paths, board_date='2026-09-09')

    def test_metadata_read_uses_injected_client_and_one_request(self):
        from boss_hire.job_setup import build_job_read_plan
        class Client:
            def operation(self, key):
                return nullcontext()
            def list_jobs(self):
                return {'code': 0, 'zpData': [{'encryptJobId': 'B', 'jobName': '岗位', 'jobOnlineStatus': 1}]}
            def job_detail(self, *_):
                raise AssertionError('list stage cannot read detail')
        plan = build_job_read_plan(account_key='test', board_date='2026-09-09')
        result = run_job_read(plan=plan, client=Client(), work_dir=self.root / 'read', generated_at=self.now().isoformat())
        self.assertEqual(result['artifact']['jobs'][0]['encrypt_job_id'], 'B')
        self.assertTrue(Path(result['artifact_path']).is_file())

    def test_job_read_freezes_consumes_authorization_before_injected_execution(self):
        from boss_hire.job_setup import build_job_read_plan
        events = []
        plan = build_job_read_plan(account_key=launcher.account_key_for(self.root / 'auth'), board_date='2026-09-09')
        def execute(**kwargs):
            events.append('execute')
            frozen = json.loads((kwargs['work_dir'] / 'plan.json').read_text())
            self.assertEqual(frozen, kwargs['plan'])
            return {'artifact': {'jobs': []}}
        with patch.object(launcher, 'sync_auth_from_chrome', side_effect=lambda _: events.append('sync') or {'session_fingerprint': 'fake'}):
            with patch.object(launcher, '_execute_read_live_plan', side_effect=execute):
                result = launcher._execute_job_read_after_confirmation(plan, self.paths, self.now())
        self.assertEqual(events, ['sync', 'execute'])
        import sqlite3
        connection = sqlite3.connect(self.root / 'guard/guard.sqlite3')
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute('SELECT status FROM live_authorizations').fetchall(), [('CONSUMED',)])
        self.assertEqual(result, {'jobs': []})

    def test_generated_materials_flow_through_source_detail_score_and_favorite_offline(self):
        from tests import test_single_job_live as live_fixtures
        from boss_hire.recruiter_jobs import normalize_job_detail
        from boss_hire.favorite_registry import FavoriteRegistry
        from boss_hire.single_job_live import (run_single_job_source_collection, run_candidate_detail_collection,
                                               run_favorite_registry_sync, run_favorite_delivery, execute_favorite_candidate)
        from boss_hire.workflow_run import load_active_workflow_run
        from boss_hire.favorite_workflow import load_active_favorite_workflow_session, favorite_workflow_paths
        class Source(live_fixtures.FakeClient):
            def operation(self, key):
                return nullcontext()
            def list_jobs(self):
                response = super().list_jobs()
                response['zpData'].append({'encryptJobId': 'other-open', 'jobName': '另一岗位', 'jobOnlineStatus': 1})
                return response
        source = Source()
        self.job = normalize_job_detail({'encrypt_job_id': 'job-open', 'name': '平台招商负责人', 'online_status': 1}, source.job_detail('job-open'))
        source.detail_calls.clear()
        self.save_selected()
        launcher.prepare_job_command(launcher.parse_args(['prepare-job']), input_fn=lambda _: '确认生成岗位材料', output_fn=lambda _: None)
        account = launcher.account_key_for(self.root / 'auth')
        account_root = self.root / 'account'
        sync_client = live_fixtures.FavoriteSyncClient([{'code': 0, 'zpData': {'cardList': [], 'hasMore': False}}])
        favorite_client = live_fixtures.FavoriteClient(readbacks={'boss-b': {'code': 0, 'zpData': {'alreadyInterested': 1}}})
        stages = []
        def read(**kwargs):
            kind = kwargs['plan'].get('plan_kind', 'source_collection')
            stages.append(kind)
            common = {key: kwargs[key] for key in ('plan', 'work_dir')}
            common['generated_at'] = kwargs.get('generated_at', self.now().isoformat())
            if kind == 'candidate_details':
                return run_candidate_detail_collection(**common, client=source, parse_resume=lambda raw: raw['resume'], inventory_path=self.paths.inventory_path)
            if kind == 'favorite_registry_sync':
                return run_favorite_registry_sync(**common, client=sync_client, registry=FavoriteRegistry(account_root, account_key=account))
            return run_single_job_source_collection(**common, client=source, inventory_path=self.paths.inventory_path)
        def write(**kwargs):
            stages.append('favorite_delivery')
            return run_favorite_delivery(plan=kwargs['plan'], ledger=kwargs['ledger'], registry=kwargs['registry'],
                work_dir=kwargs['work_dir'] / 'runs' / 'injected',
                execute_candidate=lambda candidate, write, verify: execute_favorite_candidate(client=favorite_client, candidate=candidate, write_operation=write, verify_operation=verify))
        class Scorer:
            model = 'fake-model'
            def complete_json(self, *, payload, **kwargs):
                return {'candidate_id': payload['candidate']['candidate_id'],
                        'dimension_assessments': [{'id': d['id'], 'level': 'strong', 'evidence': ['负责重点商家拓展'], 'reason': '离线样例证据', 'gaps': []} for d in payload['rubric']['dimensions']],
                        'evidence': ['负责重点商家拓展'], 'gaps': [], 'risks': [], 'follow_up_questions': [], 'summary': '离线验证'}
        with ExitStack() as stack:
            stack.enter_context(patch.object(launcher, 'workflow_paths', return_value=self.paths))
            stack.enter_context(patch.object(launcher, 'sync_auth_from_chrome', return_value={'session_fingerprint': 'fake'}))
            stack.enter_context(patch.object(launcher, 'load_saved_session_fingerprint', return_value='fake'))
            stack.enter_context(patch.object(launcher, 'favorite_account_state_dir', return_value=account_root))
            stack.enter_context(patch.object(launcher, '_execute_read_live_plan', side_effect=read))
            stack.enter_context(patch.object(launcher, '_execute_favorite_live_plan', side_effect=write))
            launcher.start_command(launcher.parse_args(['start', '--config', 'default']), input_fn=lambda _: '确认来源5次', output_fn=lambda _: None, now=self.now)
            self.assertEqual(source.detail_calls, ['job:job-open'])
            self.assertEqual(load_active_workflow_run(self.paths).state['status'], 'source_complete')
            launcher.continue_command(launcher.parse_args(['continue']), input_fn=lambda _: '确认详情1人', output_fn=lambda _: None, now=self.now)
            self.assertEqual(load_active_workflow_run(self.paths).state['status'], 'detail_complete')
            stack.enter_context(patch.object(launcher, 'OpenAICompatibleJsonLlm', return_value=Scorer()))
            launcher.continue_command(launcher.parse_args(['continue']), input_fn=lambda _: '确认评分1人', output_fn=lambda _: None, now=self.now)
            self.assertEqual(load_active_workflow_run(self.paths).state['status'], 'scoring_complete')
            launcher.favorite_command(launcher.parse_args(['favorite']), input_fn=lambda _: '确认生成未收藏Top5', output_fn=lambda _: None, now=self.now)
            self.assertEqual(favorite_client.calls, [])
            answers = iter(['1', '确认收藏1人'])
            launcher.favorite_command(launcher.parse_args(['favorite']), input_fn=lambda _: next(answers), output_fn=lambda _: None, now=self.now)
        self.assertEqual(stages, ['source_collection', 'candidate_details', 'favorite_registry_sync', 'favorite_delivery'])
        self.assertEqual(favorite_client.calls, [('write', 'boss-b'), ('read', 'boss-b')])
        session = load_active_favorite_workflow_session(favorite_workflow_paths(account_root))
        self.assertEqual(session.state['status'], 'completed')

    def test_smoke_config_runs_one_search_one_detail_and_one_score_offline(self):
        from tests import test_single_job_live as live_fixtures
        from boss_hire.recruiter_jobs import normalize_job_detail
        from boss_hire.single_job_live import run_candidate_detail_collection, run_single_job_source_collection
        from boss_hire.workflow_run import load_active_workflow_run

        class Source(live_fixtures.FakeClient):
            def __init__(self):
                super().__init__()
                self.operations = []

            def operation(self, key):
                self.operations.append(key)
                return nullcontext()

        source = Source()
        self.job = normalize_job_detail(
            {'encrypt_job_id': 'job-open', 'name': '平台招商负责人', 'online_status': 1},
            source.job_detail('job-open'),
        )
        source.detail_calls.clear()
        self.save_selected()
        launcher.prepare_job_command(
            launcher.parse_args(['prepare-job']),
            input_fn=lambda _: '确认生成岗位材料',
            output_fn=lambda _: None,
            now=self.now,
        )
        stages = []

        def read(**kwargs):
            plan = kwargs['plan']
            kind = plan.get('plan_kind', 'source_collection')
            stages.append(kind)
            common = {
                'plan': plan,
                'work_dir': kwargs['work_dir'],
                'generated_at': kwargs.get('generated_at', self.now().isoformat()),
            }
            if kind == 'candidate_details':
                return run_candidate_detail_collection(
                    **common,
                    client=source,
                    parse_resume=lambda raw: raw['resume'],
                    inventory_path=self.paths.inventory_path,
                )
            return run_single_job_source_collection(
                **common,
                client=source,
                inventory_path=self.paths.inventory_path,
            )

        score_calls = []

        class Scorer:
            model = 'fake-model'

            def complete_json(self, *, payload, **_kwargs):
                score_calls.append(payload['candidate']['candidate_id'])
                return {
                    'candidate_id': payload['candidate']['candidate_id'],
                    'dimension_assessments': [
                        {
                            'id': dimension['id'],
                            'level': 'strong',
                            'evidence': ['负责重点商家拓展'],
                            'reason': '离线冒烟证据',
                            'gaps': [],
                        }
                        for dimension in payload['rubric']['dimensions']
                    ],
                    'evidence': ['负责重点商家拓展'],
                    'gaps': [],
                    'risks': [],
                    'follow_up_questions': [],
                    'summary': '离线冒烟验证',
                }

        with ExitStack() as stack:
            stack.enter_context(patch.object(launcher, 'workflow_paths', return_value=self.paths))
            stack.enter_context(patch.object(launcher, 'sync_auth_from_chrome', return_value={'session_fingerprint': 'fake'}))
            stack.enter_context(patch.object(launcher, '_execute_read_live_plan', side_effect=read))
            launcher.start_command(
                launcher.parse_args(['start', '--config', 'smoke']),
                input_fn=lambda _: '确认来源3次',
                output_fn=lambda _: None,
                now=self.now,
            )
            self.assertEqual(load_active_workflow_run(self.paths).state['status'], 'source_complete')
            launcher.continue_command(
                launcher.parse_args(['continue', '--select', '1']),
                input_fn=lambda _: '确认详情1人',
                output_fn=lambda _: None,
                now=self.now,
            )
            self.assertEqual(load_active_workflow_run(self.paths).state['status'], 'detail_complete')
            stack.enter_context(patch.object(launcher, 'OpenAICompatibleJsonLlm', return_value=Scorer()))
            launcher.continue_command(
                launcher.parse_args(['continue']),
                input_fn=lambda _: '确认评分1人',
                output_fn=lambda _: None,
                now=self.now,
            )

        self.assertEqual(stages, ['source_collection', 'candidate_details'])
        self.assertEqual(len(source.search_calls), 1)
        self.assertEqual(source.detail_calls, ['job:job-open', 'boss-b'])
        self.assertEqual(len(source.operations), 4)
        self.assertEqual(
            source.operations[:2],
            ['metadata:open-jobs', 'metadata:selected-job-detail'],
        )
        self.assertTrue(source.operations[2].startswith('source:search:'))
        self.assertTrue(source.operations[2].endswith(':page:1'))
        self.assertTrue(source.operations[3].startswith('detail:'))
        self.assertEqual(len(score_calls), 1)
        self.assertEqual(load_active_workflow_run(self.paths).state['status'], 'scoring_complete')

    def test_wrong_job_favorite_is_rejected_and_expired_session_can_be_replaced(self):
        from tests import test_favorite_workflow as favorite_fixtures
        from boss_hire.favorite_workflow_command import prepare_favorite_workflow, expire_favorite_workflow_session
        inventory = favorite_fixtures.FavoriteCandidateSnapshotTests().make_inventory()
        kwargs = dict(inventory=inventory, account_state_dir=self.root / 'favorites', account_key='0123456789abcdef',
                      job_id='job-open', job_title='岗位', rubric_version='rubric-v1', board_date='2026-09-09', created_at='2026-09-09T10:00:00+08:00')
        first = prepare_favorite_workflow(**kwargs)
        with self.assertRaisesRegex(ValueError, '岗位'):
            prepare_favorite_workflow(**{**kwargs, 'job_id': 'other'})
        expire_favorite_workflow_session(first, updated_at='2026-09-10T10:00:00+08:00')
        fresh = prepare_favorite_workflow(**{**kwargs, 'board_date': '2026-09-10', 'created_at': '2026-09-10T10:00:00+08:00'})
        self.assertNotEqual(first.state['session_id'], fresh.state['session_id'])
        self.assertEqual(fresh.state['status'], 'awaiting_sync_confirmation')

    def test_local_favorite_close_preserves_snapshot_and_refuses_attempted_delivery(self):
        from tests import test_favorite_workflow as favorite_fixtures
        from boss_hire.favorite_workflow_command import prepare_favorite_workflow, close_favorite_workflow, materialize_favorite_workflow_delivery_inputs
        inventory = favorite_fixtures.FavoriteCandidateSnapshotTests().make_inventory()
        session = prepare_favorite_workflow(inventory=inventory, account_state_dir=self.root / 'favorites', account_key='0123456789abcdef', job_id='job-open', job_title='岗位', rubric_version='rubric-v1', board_date='2026-09-09', created_at='2026-09-09T10:00:00+08:00')
        closed = close_favorite_workflow(session, note='切换岗位', updated_at='2026-09-09T10:01:00+08:00')
        self.assertEqual(closed.state['candidate_snapshot'], session.state['candidate_snapshot'])
        self.assertEqual(closed.state['status'], 'closed_by_user')
        with self.assertRaisesRegex(ValueError, '变化'):
            materialize_favorite_workflow_delivery_inputs(session)
        another = prepare_favorite_workflow(inventory=inventory, account_state_dir=self.root / 'favorites', account_key='0123456789abcdef', job_id='job-open', job_title='岗位', rubric_version='rubric-v1', board_date='2026-09-09', created_at='2026-09-09T10:02:00+08:00')
        (another.state_path.parent / 'delivery').mkdir()
        with self.assertRaisesRegex(ValueError, '交付材料'):
            close_favorite_workflow(another, note='不要重试', updated_at='2026-09-09T10:03:00+08:00')
        self.sync.assert_not_called()
        self.factory.assert_not_called()
