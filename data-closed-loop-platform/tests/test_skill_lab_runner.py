import base64
import importlib.util
import json
import io
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill-lab'))
spec = importlib.util.spec_from_file_location('lab_runner', Path(__file__).resolve().parents[1] / 'skill-lab/runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_image_and_human_correction_sent_to_real_request_adapter(self):
        result = {'summary': '测试', 'details': '仅用于适配器测试', 'candidates': [], 'missing_information': []}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self):
                return json.dumps({'id': 'test-response', 'choices': [{'message': {'tool_calls': [{'function': {
                    'name': 'submit_lab_result', 'arguments': json.dumps(result)}}]}}]}).encode()
        runtime = {'SKILL_AGENT_API_URL': 'https://api.deepseek.com/beta/chat/completions', 'LLM_API_KEY': 'test', 'SKILL_AGENT_MODEL': 'deepseek-v4-pro'}
        case = {'kind': 'problem', 'product_line': 'deebot', 'text': '问题', 'attachments': [
            {'name': 'test.png', 'mime': 'image/png', 'data': base64.b64encode(b'\x89PNG\r\n\x1a\n').decode()}]}
        with patch('credential_profiles.active_runtime_environment', return_value=runtime), \
             patch.object(runner.urllib.request, 'urlopen', return_value=Response()) as call:
            output = runner.execute(case, 'analysis', {'human_review': {'corrected_text': '人工修正'}})
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body['tool_choice'], 'auto')
        self.assertEqual(body['max_tokens'], 32768)
        self.assertEqual(call.call_args.kwargs['timeout'], 600)
        self.assertNotIn('thinking', body)  # Do not disable reasoning to work around forced tools.
        self.assertEqual(body['messages'][1]['content'][2]['type'], 'image_url')
        self.assertIn('人工修正', body['messages'][1]['content'][0]['text'])
        self.assertEqual(output['audit']['images_sent'], 1)
        self.assertTrue(output['audit']['skill_hashes'])
        runtime['SKILL_AGENT_API_URL'] = 'https://example.invalid/chat/completions'
        runtime['SKILL_AGENT_MODEL'] = 'company-model'
        with patch('credential_profiles.active_runtime_environment', return_value=runtime), \
             patch.object(runner.urllib.request, 'urlopen', return_value=Response()) as other:
            runner.execute(case, 'analysis')
        self.assertIsInstance(json.loads(other.call_args.args[0].data)['tool_choice'], dict)

    def test_corrupt_image_rejected(self):
        with self.assertRaises(RuntimeError):
            runner.attachment_content([{'name': 'x.png', 'mime': 'image/png', 'data': base64.b64encode(b'not image').decode()}])

    def test_feature_catalog_matching_normalizes_format_but_rejects_ambiguity(self):
        catalog = [
            {'feature_domain_name': '设置', 'feature_module_name': '简易模式'},
            {'feature_domain_name': '操控页', 'feature_module_name': '清洁模式'},
        ]
        self.assertEqual(runner.resolve_catalog_feature('设置/简易模式', catalog), '设置 / 简易模式')
        self.assertEqual(runner.resolve_catalog_feature('设置 ／ 简易模式入口', catalog), '设置 / 简易模式')
        self.assertEqual(runner.resolve_catalog_feature('简易模式页面', catalog), '设置 / 简易模式')
        self.assertIsNone(runner.resolve_catalog_feature('设置 / 不存在的模式', catalog))
        ambiguous = catalog + [{'feature_domain_name': '实验设置', 'feature_module_name': '简易模式'}]
        self.assertIsNone(runner.resolve_catalog_feature('简易模式入口', ambiguous))

    def test_http_400_has_safe_actionable_reason(self):
        runtime = {'SKILL_AGENT_API_URL': 'https://api.deepseek.com/chat/completions',
                   'LLM_API_KEY': 'do-not-display', 'SKILL_AGENT_MODEL': 'deepseek-v4-pro'}
        error = runner.urllib.error.HTTPError('https://api.deepseek.com', 400, 'Bad Request', {},
            io.BytesIO(json.dumps({'error': {'message': 'Thinking mode does not support this tool_choice'}}).encode()))
        with patch('credential_profiles.active_runtime_environment', return_value=runtime), \
             patch.object(runner.urllib.request, 'urlopen', side_effect=error), \
             self.assertRaisesRegex(RuntimeError, '思考模式与工具选择参数不兼容'):
            runner.execute({'kind': 'problem', 'product_line': 'deebot', 'text': '问题', 'attachments': []}, 'analysis')

    def test_all_skill_contexts_readable(self):
        for kind in ('auto', 'problem', 'demand', 'voc', 'bi'):
            self.assertTrue(runner.skill_context(kind, 'analysis')[2])
        for stage in ('definition', 'metrics'):
            self.assertTrue(runner.skill_context('problem', stage)[2])

    def test_automatic_matching_selects_one_valid_skill_path(self):
        result = {'routing': {'category': 'problem', 'skill_names': ['problem-feedback-diagnosis'],
            'reason': '材料描述单条已发生的 App 异常和复现现象', 'confidence': .94,
            'signals': ['单条问题', '包含实际结果']}, 'summary': '完成', 'details': '问题分析',
            'candidates': [], 'missing_information': []}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return json.dumps({'id': 'route-test', 'choices': [{'message': {'tool_calls': [
                {'function': {'name': 'submit_lab_result', 'arguments': json.dumps(result)}}]}}]}).encode()
        runtime = {'SKILL_AGENT_API_URL': 'https://example.invalid', 'LLM_API_KEY': 'test', 'SKILL_AGENT_MODEL': 'model'}
        with patch('credential_profiles.active_runtime_environment', return_value=runtime), \
             patch.object(runner.urllib.request, 'urlopen', return_value=Response()) as call:
            output = runner.execute({'kind': 'auto', 'product_line': 'deebot', 'text': '英文模式仍显示中文', 'attachments': []}, 'analysis')
        body = json.loads(call.call_args.args[0].data)
        self.assertIn('routing', body['tools'][0]['function']['parameters']['required'])
        self.assertEqual(output['audit']['skills'], ['problem-feedback-diagnosis'])
        self.assertEqual(output['audit']['skills_loaded_for_routing'], runner.AUTO_SKILLS)
        self.assertEqual(output['result']['routing']['category'], 'problem')
        invalid = {**result, 'routing': {**result['routing'], 'skill_names': ['coarse-demand-decomposition']}}
        with self.assertRaisesRegex(RuntimeError, 'Skill 与材料类别不一致'):
            runner.validate_result(invalid, automatic=True)

    def test_definition_request_contains_only_selected_requirement(self):
        selected = {'title': '地图备份', 'rough_requirement': '地图可恢复', 'evidence': '原始地图证据'}
        result = {'summary': '地图备份定义', 'definition': {'title': '地图备份', 'user_task': '恢复地图',
            'problem': '地图丢失', 'scenarios': ['换机时'], 'in_scope': ['地图恢复'], 'out_of_scope': ['耗材'],
            'acceptance_criteria': ['恢复后地图一致'], 'evidence': ['原始地图证据']}, 'missing_information': []}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return json.dumps({'id': 'test-only', 'choices': [{'message': {'tool_calls': [
                {'function': {'name': 'submit_lab_result', 'arguments': json.dumps(result)}}]}}]}).encode()
        runtime = {'SKILL_AGENT_API_URL': 'https://example.invalid', 'LLM_API_KEY': 'test', 'SKILL_AGENT_MODEL': 'model'}
        with patch('credential_profiles.active_runtime_environment', return_value=runtime), \
             patch.object(runner.urllib.request, 'urlopen', return_value=Response()) as call:
            output = runner.execute({'kind': 'voc', 'product_line': 'deebot', 'text': '无关整批材料',
                'attachments': [{'invalid': '应被排除'}]}, 'definition',
                {'selected_requirement': selected, 'candidates': [{'title': '无关耗材需求'}], 'summary': '无关批次摘要'})
        context = json.loads(json.loads(call.call_args.args[0].data)['messages'][1]['content'])
        self.assertNotIn('无关', json.dumps(context, ensure_ascii=False))
        self.assertEqual(output['result']['selected_requirement'], selected)
        self.assertIn('definition', output['result'])
