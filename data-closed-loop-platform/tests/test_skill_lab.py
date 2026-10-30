import importlib.util
import tempfile
import time
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill-lab'))
spec = importlib.util.spec_from_file_location('lab_server', Path(__file__).resolve().parents[1] / 'skill-lab/server.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'lab.sqlite3'
        self.store = lab.Store(self.path, lambda case, stage, parent: {
            'result': {'summary': '测试', 'candidates': [{'title': '英文文案修复'}], 'details': '详情'},
            'audit': {'model': 'fake-test-only'}})
        self.case = self.store.create_case({'title': '窗帘', 'kind': 'problem', 'product_line': 'deebot',
            'text': '英文模式仍显示中文', 'attachments': []})

    def tearDown(self):
        self.tmp.cleanup()

    def run_stage(self, stage='analysis', parent=None):
        payload = {'case_id': self.case['id'], 'stage': stage}
        if parent:
            payload['parent_run_id'] = parent
            payload['pool_item_id'] = self.store.state()['pool'][0]['id']
        run = self.store.start_run(payload)
        for _ in range(200):
            run = next(r for r in self.store.state()['runs'] if r['id'] == run['id'])
            if run['status'] in ('completed', 'failed'):
                return run
            time.sleep(.01)
        self.fail('run did not finish')

    def test_review_and_downstream_persist(self):
        run = self.run_stage()
        self.assertEqual(run['status'], 'completed')
        with self.assertRaises(lab.LabError):
            self.store.start_run({'case_id': self.case['id'], 'stage': 'definition', 'parent_run_id': run['id']})
        self.store.review({'run_id': run['id'], 'decision': 'accept', 'note': '可用', 'corrected_text': '修正文案'})
        self.assertEqual(len(self.store.state()['pool']), 1)
        self.assertEqual(self.run_stage('definition', run['id'])['status'], 'completed')
        self.assertEqual(len(lab.Store(self.path).state()['pool']), 1)
        with self.assertRaises(lab.LabError):
            self.store.review({'run_id': run['id'], 'decision': 'reject', 'note': ''})

    def test_reruns_keep_original(self):
        a, b = self.run_stage(), self.run_stage()
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(len(self.store.state()['runs']), 2)
        self.assertEqual(len(self.store.state()['pool']), 2)

    def test_success_automatically_publishes_without_human_review(self):
        run = self.run_stage()
        self.assertNotIn('review', run)
        self.assertEqual(run['pool_publication']['count'], 1)
        item = self.store.state()['pool'][0]
        self.assertEqual(item['run_id'], run['id'])
        self.assertEqual(item['candidate']['title'], '英文文案修复')
        self.assertEqual(self.run_stage('definition', run['id'])['status'], 'completed')
        self.assertEqual(len(lab.Store(self.path).state()['pool']), 1)
        self.assertEqual(len(lab.Store(self.path).state()['pool']), 1)

    def test_zero_candidates_is_success_without_pool_items(self):
        self.store.executor = lambda *args: {'result': {'summary': '无候选', 'candidates': []}, 'audit': {}}
        run = self.run_stage()
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['pool_publication']['count'], 0)
        self.assertEqual(self.store.state()['pool'], [])

    def test_malformed_candidates_do_not_partially_enter_pool(self):
        self.store.executor = lambda *args: {'result': {'candidates': [{'title': '合法'}, None]}, 'audit': {}}
        run = self.run_stage()
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(self.store.state()['pool'], [])

    def test_legacy_success_backfill_preserves_rejections(self):
        with self.store.db() as db:
            for ident, review in [('legacy-pending', None), ('legacy-rejected', {'decision': 'reject'})]:
                value = {'id': ident, 'case_id': self.case['id'], 'stage': 'analysis',
                    'status': 'completed', 'created_at': '2026-10-29',
                    'result': {'candidates': [{'title': ident}]}, 'audit': {}}
                if review:
                    value['review'] = review
                self.store.save(db, 'runs', value)
        restored = lab.Store(self.path).state()
        self.assertEqual([x['run_id'] for x in restored['pool']], ['legacy-pending'])
        self.assertEqual(len(lab.Store(self.path).state()['pool']), 1)

    def test_failure_not_success(self):
        def fail(*args):
            raise RuntimeError('provider error')
        self.store.executor = fail
        self.assertEqual(self.run_stage()['status'], 'failed')
        self.assertEqual(self.store.state()['pool'], [])

    def test_invalid_attachment(self):
        with self.assertRaises(lab.LabError):
            self.store.create_case({'title': 'bad', 'kind': 'problem', 'product_line': 'deebot', 'text': 'x',
                'attachments': [{'name': 'x.png', 'mime': 'image/png', 'data': 'invalid'}]})

    def test_unified_intake_defaults_to_automatic_matching(self):
        case = self.store.create_case({'title': '自动入口', 'product_line': 'deebot',
            'text': '这里可以是问题、需求、VOC 或指标材料', 'attachments': []})
        self.assertEqual(case['kind'], 'auto')

    def test_pool_scope_batch_and_metric_binding(self):
        captured = []
        def executor(case, stage, parent):
            captured.append((case, stage, parent))
            return {'result': {'summary': '测试', 'candidates': [{'title': '地图备份', 'evidence': '地图证据'},
                {'title': '耗材寿命', 'evidence': '耗材证据'}], 'definition': {'title': '地图备份'}}, 'audit': {}}
        self.store.executor = executor
        analysis = self.run_stage()
        self.store.review({'run_id': analysis['id'], 'decision': 'accept', 'note': '', 'corrected_text': '整批修订不可泄漏'})
        first, second = self.store.state()['pool']
        batch = self.store.run_pool_batch({'pool_item_ids': [first['id'], 'missing'], 'stage': 'definition'})
        self.assertEqual(len(batch['runs']), 1)
        self.assertEqual(len(batch['errors']), 1)
        ident = batch['runs'][0]['id']
        for _ in range(200):
            run = next(r for r in self.store.state()['runs'] if r['id'] == ident)
            if run['status'] == 'completed': break
            time.sleep(.01)
        case, stage, parent = captured[-1]
        self.assertEqual(case['attachments'], [])
        self.assertEqual(set(parent), {'selected_requirement', 'pool_item_id'})
        self.assertEqual(parent['selected_requirement']['title'], first['candidate']['title'])
        self.assertNotIn('整批修订', case['text'])
        self.assertNotIn(second['candidate']['title'], case['text'])
        self.store.review({'run_id': ident, 'decision': 'accept', 'note': ''})
        with self.assertRaises(lab.LabError):
            self.store.start_run({'case_id': self.case['id'], 'stage': 'metrics', 'parent_run_id': ident, 'pool_item_id': second['id']})

    def test_completed_definition_enters_requirement_section_before_review(self):
        analysis = self.run_stage()
        self.store.review({'run_id': analysis['id'], 'decision': 'accept', 'note': ''})
        item = self.store.state()['pool'][0]
        self.assertEqual(self.store.state()['requirements'], [])
        self.store.executor = lambda *args: {'result': {'definition': {'title': '英文文案修复'}}, 'audit': {}}
        run = self.run_stage('definition', analysis['id'])
        requirement = self.store.state()['requirements'][0]
        self.assertEqual(requirement['id'], item['id'])
        self.assertEqual(requirement['definition_review_status'], 'pending')
        self.assertEqual(len(self.store.state()['pool']), 1)  # Canonical evidence preserved.
        self.store.review({'run_id': run['id'], 'decision': 'reject', 'note': '需修改'})
        self.assertEqual(self.store.state()['requirements'][0]['definition_review_status'], 'reject')
        self.store.executor = lambda *args: (_ for _ in ()).throw(RuntimeError('test failure'))
        self.run_stage('definition', analysis['id'])
        self.assertEqual(self.store.state()['requirements'][0]['definition_run_id'], run['id'])
        self.assertEqual(len(lab.Store(self.path).state()['requirements']), 1)


if __name__ == '__main__':
    unittest.main()
