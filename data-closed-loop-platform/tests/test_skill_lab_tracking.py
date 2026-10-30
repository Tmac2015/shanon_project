import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill-lab'))
from tracking import TrackingStore, TrackingError, sample_payload


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'tracking.json'
        self.store = TrackingStore(self.path)
        self.item = self.store.import_payload(sample_payload())

    def tearDown(self):
        self.tmp.cleanup()

    def update(self, action, **kw):
        return self.store.update({'id': self.item['id'], 'action': action, **kw})

    def prepare(self):
        self.update('assign', owner='商清跨端核查负责人', note='人工指定')
        self.update('freeze', target_rate=.85, min_sample=200, observation_days=7,
                    definition='成功次数 / 有效尝试数，mapping_attempt_id 去重')
        self.update('release', version='SIM-FIX-1', date='2026-10-29', note='仅模拟修复')

    def observe(self, **override):
        data = dict(stat_date='2026-11-04', attempt_count=200, success_count=180,
                    window_days=7, definition='成功次数 / 有效尝试数，mapping_attempt_id 去重',
                    version='SIM-FIX-1', note='模拟结果')
        data.update(override)
        return self.update('observe', **data)

    def test_baseline_identity_and_persistence(self):
        baseline = self.item['baseline']
        self.assertEqual((baseline['rate'], baseline['gap_pp'], baseline['decline_pp']), (.68, 17, 19))
        self.assertEqual(self.item['owner'], '')
        self.assertEqual(self.item['diagnosis']['routing'], '待人工分配')
        self.assertEqual(self.store.import_payload(sample_payload())['id'], self.item['id'])
        self.assertEqual(TrackingStore(self.path).list()[0]['raw_payload'], sample_payload())
        modified = sample_payload()
        modified['input'] = 'changed'
        with self.assertRaises(TrackingError):
            self.store.import_payload(modified)
        self.assertEqual(len(self.store.list()), 1)

    def test_accepted_metric_creates_idempotent_linked_draft_without_guessing(self):
        context = {'product_line': 'deebot', 'requirement_title': '简易模式入口可发现',
            'feature': '设置 / 简易模式',
            'lineage': {'source_record_id': 'case-1', 'analysis_run_id': 'analysis-1',
                'candidate_id': 'candidate-1', 'requirement_id': 'candidate-1',
                'definition_run_id': 'definition-1', 'metric_run_id': 'metric-1'},
            'metrics': [{'name': '入口发现成功率', 'role': '主指标', 'definition': '找到入口的用户数/有效任务用户数',
                'formula': 'found_users / valid_users', 'data_source': '可用性测试', 'baseline': '待测',
                'target': '≥80%', 'validation': '至少30名用户'}]}
        first = self.store.create_from_metric_run(context)[0]
        second = self.store.create_from_metric_run(context)[0]
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['status'], 'pending_configuration')
        self.assertIsNone(first['baseline']['rate'])
        self.assertIsNone(first['metric_card'])
        self.assertEqual(first['lineage']['requirement_id'], 'candidate-1')
        self.assertEqual(first['metric_suggestion']['target'], '≥80%')
        self.assertEqual(len(self.store.list()), 2)  # Existing BI fixture + linked draft.

    def test_invalid_input_never_persists(self):
        for key, value in [('mapping_success_rate', .9), ('mapping_attempt_count', 0),
                           ('mapping_success_count', 201), ('threshold', float('nan')),
                           ('is_simulation', False), ('mapping_success_count', True)]:
            payload = sample_payload()
            payload['form']['data'][0][key] = value
            with self.subTest(key=key), self.assertRaises(TrackingError):
                self.store.import_payload(payload)
        payload = sample_payload()
        payload['form']['data'].append(copy.deepcopy(payload['form']['data'][0]))
        with self.assertRaises(TrackingError):
            self.store.import_payload(payload)
        self.assertEqual(len(self.store.list()), 1)

    def test_can_record_evidence_without_forcing_owner(self):
        item = self.update('assign', owner='', note='跨端责任不明，等待核查')
        self.assertEqual(item['owner'], '')
        self.assertEqual(item['status'], 'pending_assignment')
        self.assertEqual(item['diagnosis']['routing'], '待人工分配')
        self.assertEqual(item['history'][-1]['note'], '跨端责任不明，等待核查')

    def test_closed_loop_requires_model_run_and_explicit_human_decision(self):
        with self.assertRaises(TrackingError):
            self.update('release', version='X', date='2026-10-29')
        self.prepare()
        with self.assertRaises(TrackingError):
            self.update('freeze', target_rate=.7, min_sample=1, observation_days=1, definition='x')
        item = self.observe()
        self.assertEqual(item['observations'][-1]['status'], 'met')
        self.assertEqual(item['status'], 'awaiting_review')
        with self.assertRaises(TrackingError):
            self.update('close', decision='achieved', note='人工确认')
        run, snapshot = self.store.start_retrospective(self.item['id'])
        with self.assertRaises(TrackingError):
            self.store.start_retrospective(self.item['id'])
        self.assertEqual(snapshot['observations'][-1]['rate'], .9)
        self.assertEqual(snapshot['investigation_notes'][0]['note'], '人工指定')
        self.store.finish_retrospective(self.item['id'], run['id'], output={
            'result': {'actual_result_summary': '测试模型结果'}, 'audit': {'model': 'test-only'}})
        item = self.update('close', decision='achieved', note='人工确认模拟达成')
        self.assertEqual(item['status'], 'closed')
        with self.assertRaises(TrackingError):
            self.update('assign', owner='other')

    def test_inconclusive_and_latest_observation_binding(self):
        self.prepare()
        for override in [dict(attempt_count=100, success_count=90), dict(window_days=3),
                         dict(definition='不同口径'), dict(version='其他版本')]:
            item = self.observe(**override)
            self.assertEqual(item['observations'][-1]['status'], 'inconclusive')
            self.assertTrue(item['observations'][-1]['reasons'])
        with self.assertRaises(TrackingError):
            self.observe(stat_date='2026-10-28')
        self.assertEqual(self.observe(success_count=160)['observations'][-1]['status'], 'not_met')
        with self.assertRaises(TrackingError):
            self.update('close', decision='iterate', note='未复盘不得继续优化')
        run, _ = self.store.start_retrospective(self.item['id'])
        self.store.finish_retrospective(self.item['id'], run['id'], output={'result': {}, 'audit': {}})
        self.assertEqual(self.update('close', decision='iterate', note='继续优化')['status'], 'tracking')
        self.observe()
        run, _ = self.store.start_retrospective(self.item['id'])
        self.store.finish_retrospective(self.item['id'], run['id'], output={'result': {}, 'audit': {}})
        self.observe()
        with self.assertRaises(TrackingError):
            self.update('close', decision='achieved', note='旧复盘不能用于新观察')

    def test_restart_failed_run_and_retry(self):
        self.prepare()
        self.observe()
        run, _ = self.store.start_retrospective(self.item['id'])
        store = TrackingStore(self.path)
        self.assertEqual(store.list()[0]['retrospectives'][0]['status'], 'failed')
        rerun, _ = store.start_retrospective(self.item['id'])
        store.finish_retrospective(self.item['id'], rerun['id'], error='provider failed')
        self.assertEqual(store.list()[0]['retrospectives'][-1]['status'], 'failed')
        with self.assertRaises(TrackingError):
            store.finish_retrospective(self.item['id'], run['id'], output={'result': {}, 'audit': {}})

    def test_json_pull_is_idempotent_and_persists_failures(self):
        self.prepare()
        content = json.dumps({'records': [{'source_record_id': 'obs-1', 'stat_date': '2026-11-04',
            'attempt_count': 200, 'success_count': 180, 'window_days': 7,
            'definition': '成功次数 / 有效尝试数，mapping_attempt_id 去重',
            'version': 'SIM-FIX-1', 'note': '模拟拉取'}]})
        self.update('configure_source', source_format='json', schedule='daily', content=content,
                    note='测试计划')
        pulled = self.store.pull(self.item['id'])
        self.assertEqual(len(pulled['observations']), 1)
        self.assertEqual(pulled['observations'][0]['ingestion_mode'], 'manual_pull')
        pulled = self.store.pull(self.item['id'])
        self.assertEqual(len(pulled['observations']), 1)
        self.assertEqual(pulled['pull_runs'][-1]['skipped'], 1)
        self.assertIsNotNone(pulled['source_config']['next_pull_at'])

        broken = json.dumps({'records': [{'source_record_id': 'obs-2', 'stat_date': '2026-11-05',
            'attempt_count': 200, 'success_count': 180, 'window_days': 7,
            'definition': '错误口径', 'version': '', 'note': '坏数据'}]})
        self.update('configure_source', source_format='json', schedule='manual', content=broken,
                    note='替换为坏数据')
        with self.assertRaises(TrackingError):
            self.store.pull(self.item['id'])
        saved = self.store.list()[0]
        self.assertEqual(saved['pull_runs'][-1]['status'], 'failed')
        self.assertEqual(len(saved['observations']), 1)


if __name__ == '__main__':
    unittest.main()
