"""HTTP-level acceptance in temporary storage; never changes workbench cases."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill-lab'))
from server import create_server
from tracking import sample_payload


class TrackingAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        def executor(case, stage, parent):
            if stage == 'analysis':
                result = {'summary': '分析完成', 'details': '测试证据', 'missing_information': [],
                    'candidates': [{'title': '简易模式入口可发现', 'problem_statement': '入口难找',
                        'user_task': '找到简易模式', 'rough_requirement': '让入口可发现',
                        'feature': '设置 / 简易模式', 'feature_basis': '现有功能目录',
                        'evidence': '用户找不到入口', 'missing_information': []}]}
            elif stage == 'definition':
                result = {'summary': '定义完成', 'missing_information': [], 'definition': {
                    'title': '简易模式入口可发现', 'user_task': '找到并开启简易模式', 'problem': '入口难找',
                    'scenarios': ['首次使用'], 'in_scope': ['入口导航'], 'out_of_scope': ['清洁算法'],
                    'acceptance_criteria': ['目标用户可找到入口'], 'evidence': ['用户找不到入口']}}
            else:
                result = {'summary': '指标完成', 'missing_information': [], 'metrics': [{
                    'name': '入口发现成功率', 'role': '主指标',
                    'definition': '找到入口的用户数/有效任务用户数', 'formula': 'found/valid',
                    'data_source': '可用性测试', 'baseline': '待测', 'target': '≥80%',
                    'validation': '至少30名目标用户'}]}
            return {'result': result, 'audit': {'model': 'fake-test-only'}}
        self.server = create_server(0, Path(self.temp.name) / 'lab.sqlite3', executor)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.worker.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, body=None, origin=True):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', **({'Origin': self.base} if origin else {})})
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return json.loads(raw) if 'application/json' in response.headers['Content-Type'] else raw

    def test_http_import_security_and_closed_loop(self):
        self.assertIn(b'TrackingLab', self.request('/tracking.js'))
        payload = sample_payload()
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request('/api/tracking/import', {'payload': payload}, origin=False)
        self.assertEqual(denied.exception.code, 403)
        item = self.request('/api/tracking/import', {'payload': payload})
        self.assertEqual(self.request('/api/tracking/import', {'payload': payload})['id'], item['id'])
        self.assertEqual(item['baseline']['gap_pp'], 17)
        self.assertEqual(self.request('/api/state')['pool'], [])

        def update(action, **fields):
            return self.request('/api/tracking/update', {'id': item['id'], 'action': action, **fields})
        update('assign', owner='测试隔离环境核查人', note='不代表真实人员安排')
        update('freeze', target_rate=.85, min_sample=200, observation_days=7, definition='测试去重口径')
        update('release', version='SIM-TEST-1', date='2026-10-29', note='模拟上线')
        update('observe', stat_date='2026-11-04', attempt_count=200, success_count=180,
            window_days=7, definition='测试去重口径', version='SIM-TEST-1', note='测试模拟结果')
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            update('close', decision='achieved', note='不能跳过复盘')
        self.assertEqual(blocked.exception.code, 400)
        output = {'result': {'actual_result_summary': '测试模拟窗口达标', 'reason_summary': '原因未证实',
            'next_action': '人工核查', 'missing_information': ['原始日志']},
            'audit': {'execution_mode': 'llm_agent', 'provider_response_id': 'test-only',
                      'skills': ['launch-effect-retrospective']}}
        with patch('tracking_runner.execute_retrospective', return_value=output):
            self.request('/api/tracking/retrospective', {'id': item['id']})
            for _ in range(100):
                saved = self.request('/api/tracking')['items'][0]
                if saved['retrospectives'][-1]['status'] != 'running':
                    break
                time.sleep(.01)
        self.assertEqual(saved['retrospectives'][-1]['status'], 'completed')
        self.assertEqual(update('close', decision='achieved', note='测试人工确认')['status'], 'closed')
        self.assertEqual(self.request('/api/tracking')['items'][0]['status'], 'closed')

    def test_accepted_metric_auto_creates_linked_tracking_draft(self):
        case = self.request('/api/cases', {'title': '入口问题', 'product_line': 'deebot',
            'text': '用户找不到简易模式入口', 'attachments': []})
        analysis = self.request('/api/run', {'case_id': case['id'], 'stage': 'analysis'})

        def finished(run_id):
            for _ in range(100):
                run = next(r for r in self.request('/api/state')['runs'] if r['id'] == run_id)
                if run['status'] in ('completed', 'failed'):
                    return run
                time.sleep(.01)
            self.fail('run did not finish')

        analysis = finished(analysis['id'])
        pool_id = self.request('/api/state')['pool'][0]['id']
        definition = self.request('/api/pool/run', {'pool_item_ids': [pool_id], 'stage': 'definition'})['runs'][0]
        definition = finished(definition['id'])
        self.request('/api/review', {'run_id': definition['id'], 'decision': 'accept', 'note': ''})
        metric = self.request('/api/pool/run', {'pool_item_ids': [pool_id], 'stage': 'metrics'})['runs'][0]
        metric = finished(metric['id'])
        reviewed = self.request('/api/review', {'run_id': metric['id'], 'decision': 'accept', 'note': ''})
        self.assertEqual(reviewed['tracking_publication']['status'], 'published')
        self.assertEqual(reviewed['tracking_publication']['count'], 1)
        state = self.request('/api/state')
        self.assertEqual(len(state['tracking']), 1)
        linked = state['tracking'][0]
        self.assertEqual(linked['lineage']['source_record_id'], case['id'])
        self.assertEqual(linked['lineage']['requirement_id'], pool_id)
        self.assertEqual(linked['lineage']['metric_run_id'], metric['id'])
        self.assertEqual(linked['status'], 'pending_configuration')
        full = self.request('/api/tracking')['items'][0]
        self.assertIsNone(full['baseline']['rate'])
        self.assertEqual(full['metric_suggestion']['target'], '≥80%')

        def update(action, **fields):
            return self.request('/api/tracking/update', {'id': full['id'], 'action': action, **fields})
        update('assign', owner='测试产品负责人', note='仅用于隔离验收')
        definition_text = '找到入口的用户数/有效任务用户数'
        update('freeze', target_rate=.8, min_sample=30, observation_days=7, definition=definition_text)
        update('release', version='SIM-UX-1', date='2026-10-30', note='模拟上线')
        update('observe', stat_date='2026-11-05', attempt_count=30, success_count=20,
            window_days=7, definition=definition_text, version='SIM-UX-1', note='未达到目标')
        retro_output = {'result': {'actual_result_summary': '发现成功率未达标',
            'reason_summary': '入口仍不够明显', 'next_action': '继续优化入口层级与标签',
            'missing_information': []}, 'audit': {'model': 'fake-test-only'}}
        with patch('tracking_runner.execute_retrospective', return_value=retro_output):
            self.request('/api/tracking/retrospective', {'id': full['id']})
            for _ in range(100):
                tracked = self.request('/api/tracking')['items'][0]
                if tracked['retrospectives'][-1]['status'] != 'running':
                    break
                time.sleep(.01)
        tracked = update('close', decision='iterate', note='未达标，进入下一轮')
        self.assertEqual(tracked['closure']['writeback']['status'], 'applied')
        iteration_id = tracked['closure']['writeback']['iteration_requirement_id']
        iteration = next(item for item in self.request('/api/state')['pool'] if item['id'] == iteration_id)
        self.assertEqual(iteration['entry_mode'], 'retrospective_iteration')
        self.assertEqual(iteration['parent_requirement_id'], pool_id)
        self.assertEqual(iteration['candidate']['rough_requirement'], '继续优化入口层级与标签')


if __name__ == '__main__':
    unittest.main()
