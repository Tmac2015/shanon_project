import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1] / 'skill-lab'
sys.path.insert(0, str(LAB))
spec = importlib.util.spec_from_file_location('tracking_runner', LAB / 'tracking_runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
import credential_profiles


class TrackingRunnerTests(unittest.TestCase):
    def item(self):
        return {'id': 'sim-1', 'is_simulation': True, 'title': '商清建图', 'owner': '',
            'metric_card': {'version': 'v1', 'target_rate': .85},
            'release': {'version': 'SIM-FIX-1', 'date': '2026-11-01'},
            'observations': [{'id': 'obs-1', 'rate': .9, 'status': 'met'}],
            'raw_payload': {'content': '不应作为指令进入复盘'}}

    def result(self):
        return {'actual_result_summary': '模拟窗口达到冻结标准。',
                'reason_summary': '缺少失败阶段证据，原因仍待验证。',
                'next_action': '核对日志并人工确认结果。', 'missing_information': ['缺少失败阶段日志。']}

    def response(self, result=None, response_id='test-tracking-response'):
        payload = {'id': response_id, 'choices': [{'message': {'tool_calls': [{'function': {
            'name': 'submit_tracking_retrospective', 'arguments': json.dumps(result or self.result())}}]}}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return json.dumps(payload).encode()
        return Response()

    def test_real_adapter_receives_only_frozen_facts_and_preserves_item(self):
        item = self.item()
        before = copy.deepcopy(item)
        runtime = {'SKILL_AGENT_API_URL': 'https://api.deepseek.com/chat/completions',
                   'SKILL_AGENT_MODEL': 'deepseek-v4-pro', 'LLM_API_KEY': 'test-secret'}
        with patch('credential_profiles.active_runtime_environment', return_value=runtime), \
             patch.object(runner.urllib.request, 'urlopen', return_value=self.response()) as request:
            output = runner.execute_retrospective(item)
        self.assertEqual(item, before)
        body = json.loads(request.call_args.args[0].data)
        context = json.loads(body['messages'][1]['content'])
        self.assertNotIn('raw_payload', context)
        self.assertEqual(context['metric_card']['target_rate'], .85)
        self.assertEqual(context['observations'][0]['status'], 'met')
        self.assertEqual(body['tool_choice'], 'auto')
        self.assertEqual(body['max_tokens'], 32768)
        self.assertEqual(request.call_args.kwargs['timeout'], 600)
        self.assertTrue(output['audit']['skill_hashes'])
        self.assertEqual(output['audit']['skills'], ['launch-effect-retrospective'])
        self.assertNotIn('owner', output['result'])

    def test_company_gateway_uses_named_tool(self):
        runtime = {'SKILL_AGENT_API_URL': 'https://company.invalid/chat/completions',
                   'SKILL_AGENT_MODEL': 'company', 'LLM_API_KEY': 'test-secret'}
        with patch('credential_profiles.active_runtime_environment', return_value=runtime), \
             patch.object(runner.urllib.request, 'urlopen', return_value=self.response()) as request:
            runner.execute_retrospective(self.item())
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body['tool_choice']['function']['name'], 'submit_tracking_retrospective')
        self.assertEqual(request.call_args.kwargs['timeout'], 180)

    def test_missing_observations_or_unsimulated_data_block_before_call(self):
        with patch.object(runner.urllib.request, 'urlopen') as request:
            for item in ({}, dict(self.item(), observations=[]), dict(self.item(), is_simulation=False)):
                with self.assertRaises(RuntimeError):
                    runner.execute_retrospective(item)
            request.assert_not_called()

    def test_unavailable_runtime_fails_closed(self):
        with patch.dict(runner.os.environ, {}, clear=True), \
             patch('credential_profiles.active_runtime_environment', return_value=None), \
             self.assertRaisesRegex(RuntimeError, '未生成替代'):
            runner.execute_retrospective(self.item())

    def test_unstructured_or_unauditable_response_fails_closed(self):
        runtime = {'SKILL_AGENT_API_URL': 'https://company.invalid',
                   'SKILL_AGENT_MODEL': 'company', 'LLM_API_KEY': 'test-secret'}
        for response in (self.response({'owner': '不允许'}), self.response(response_id='')):
            with patch('credential_profiles.active_runtime_environment', return_value=runtime), \
                 patch.object(runner.urllib.request, 'urlopen', return_value=response), \
                 self.assertRaises(RuntimeError):
                runner.execute_retrospective(self.item())


if __name__ == '__main__':
    unittest.main()
