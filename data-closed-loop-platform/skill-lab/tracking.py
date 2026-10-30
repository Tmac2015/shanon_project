"""Deterministic, simulation-only BI tracking. No inferred App ownership or AI claims."""
import copy
import csv
import hashlib
import io
import json
import math
import os
import tempfile
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


class TrackingError(ValueError):
    pass


def _text(value, label, limit=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TrackingError(f'{label}不能为空且不能超过 {limit} 字符')
    return value.strip()


def _integer(value, label, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TrackingError(f'{label}必须是至少 {minimum} 的整数')
    return value


def _rate(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise TrackingError(f'{label}必须是 0 到 1 之间的有限数值')
    return value


def _date(value, label):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise TrackingError(f'{label}必须是 YYYY-MM-DD 日期') from None


def _counts(attempt, success):
    attempt = _integer(attempt, '有效尝试数')
    success = _integer(success, '成功次数', 0)
    if success > attempt:
        raise TrackingError('成功次数不能大于有效尝试数')
    return attempt, success, success / attempt


def _now():
    return datetime.now(timezone.utc).isoformat()


def _source_records(content, source_format):
    if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 1024 * 1024:
        raise TrackingError('数据源内容不能为空且不能超过 1 MB')
    try:
        if source_format == 'json':
            value = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            records = value.get('records') if isinstance(value, dict) else value
        elif source_format == 'csv':
            records = list(csv.DictReader(io.StringIO(content)))
        else:
            raise TrackingError('数据源格式仅支持 json 或 csv')
    except (ValueError, TypeError, csv.Error):
        raise TrackingError('数据源内容无法按所选格式解析') from None
    if not isinstance(records, list) or not records or len(records) > 500:
        raise TrackingError('数据源须包含 1–500 条观测记录')
    result = []
    for index, raw in enumerate(records, 1):
        if not isinstance(raw, dict):
            raise TrackingError(f'第 {index} 条观测不是对象')
        try:
            normalized = {
                'stat_date': raw.get('stat_date'),
                'attempt_count': int(raw.get('attempt_count')),
                'success_count': int(raw.get('success_count')),
                'window_days': int(raw.get('window_days')),
                'version': raw.get('version'),
                'definition': raw.get('definition'),
                'note': raw.get('note', ''),
            }
        except (TypeError, ValueError):
            raise TrackingError(f'第 {index} 条观测的样本数或观察天数不是整数') from None
        normalized['source_record_id'] = str(raw.get('source_record_id') or hashlib.sha256(
            json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        result.append(normalized)
    if len({row['source_record_id'] for row in result}) != len(result):
        raise TrackingError('数据源中的 source_record_id 不能重复')
    return result


def _next_pull(schedule, moment=None):
    moment = moment or datetime.now(timezone.utc)
    return (moment + timedelta(days=1 if schedule == 'daily' else 7)).isoformat()


def sample_payload():
    return {'form': {'data': [{
        'product_line': 'deebot', 'business_scope': '商清', 'metric_name': '建图成功率',
        'stat_date': '2026-10-28', 'mapping_attempt_count': 200, 'mapping_success_count': 136,
        'mapping_success_rate': .68, 'threshold': .85, 'previous_period_rate': .87,
        'is_simulation': True,
    }], 'params': [
        {'name': 'source_type', 'value': 'bi_alert'},
        {'name': 'source_id', 'value': 'SIM-BI-DEEBOT-MAPPING-001'},
        {'name': 'content', 'value': '【联调模拟，非真实告警】地宝商清建图成功率偏低。2026-10-28统计窗口内，共发生200次有效建图尝试，成功136次，成功率68%，低于模拟阈值85%，较上一周期87%下降19个百分点。指标口径：成功完成建图的尝试次数÷有效建图尝试次数，同一次尝试按mapping_attempt_id去重。当前未提供失败阶段、错误码、设备型号、固件及App版本分布，尚不能判断异常来自设备、算法、网络还是App。请匹配核查负责人，确认数据有效性、影响范围及后续处理方向，不要直接认定为App开发需求。'},
        {'name': 'extra_context', 'value': '本条仅用于验证BI告警接入及负责人匹配。产品线为地宝，业务范围为商清；建图指标涉及跨端链路，不应仅因用户从App发起建图就判定为App责任。归属不明确时返回待人工分配，不强行派给李星瑞。'},
    ]}, 'input': None}


class TrackingStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        items = self._read()
        changed = False
        for item in items:
            for run in item.get('retrospectives', []):
                if run['status'] in ('queued', 'running'):
                    run.update(status='failed', error='服务重启导致运行中断，请重新运行', finished_at=_now())
                    changed = True
        if changed:
            self._write(items)

    def _read(self):
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(value, list):
                raise ValueError()
            return value
        except (ValueError, OSError):
            raise TrackingError('Tracking 存储读取失败；未覆盖原始数据') from None

    def _write(self, items):
        fd, name = tempfile.mkstemp(prefix='.tracking-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(items, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def list(self):
        with self.lock:
            return copy.deepcopy(self._read())

    def create_from_metric_run(self, context):
        """Create idempotent Tracking drafts from an accepted metric run.

        Metric outputs are intentionally kept as strings by the Skill contract.  We
        therefore preserve them as suggestions and do not guess a numeric baseline,
        target, sample size or observation window.  Those values are frozen by a
        human in the Tracking step.
        """
        if not isinstance(context, dict):
            raise TrackingError('指标链路上下文必须是对象')
        lineage = context.get('lineage')
        metrics = context.get('metrics')
        if not isinstance(lineage, dict) or not isinstance(metrics, list) or not metrics:
            raise TrackingError('已采纳指标缺少需求链路或指标内容')
        required_lineage = ('source_record_id', 'analysis_run_id', 'candidate_id',
                            'requirement_id', 'definition_run_id', 'metric_run_id')
        if any(not isinstance(lineage.get(key), str) or not lineage[key].strip()
               for key in required_lineage):
            raise TrackingError('指标链路字段不完整，未创建 Tracking 草稿')
        product = _text(context.get('product_line'), '产品线', 100)
        requirement_title = _text(context.get('requirement_title'), '需求标题', 300)
        created = []
        with self.lock:
            items = self._read()
            for index, metric in enumerate(metrics):
                if not isinstance(metric, dict):
                    raise TrackingError('指标内容格式错误')
                suggestion = {key: _text(metric.get(key), f'指标{key}', 10000)
                              for key in ('name', 'role', 'definition', 'formula',
                                          'data_source', 'baseline', 'target', 'validation')}
                source_id = f"requirement_metric:{lineage['metric_run_id']}:{index}"
                existing = next((item for item in items if item.get('source_id') == source_id), None)
                if existing:
                    if existing.get('lineage') != lineage or existing.get('metric_suggestion') != suggestion:
                        raise TrackingError('同一指标运行已存在不同 Tracking 内容；原记录未覆盖')
                    created.append(copy.deepcopy(existing))
                    continue
                now = _now()
                ident = hashlib.sha256(source_id.encode()).hexdigest()[:32]
                item = {
                    'id': ident,
                    'title': f"{requirement_title} · {suggestion['name']}",
                    'source_id': source_id,
                    'source_kind': 'requirement_metric',
                    'product_line': product,
                    'business_scope': context.get('feature') or '需求指标验证',
                    'is_simulation': True,
                    'status': 'pending_configuration',
                    'created_at': now,
                    'lineage': copy.deepcopy(lineage),
                    'metric_suggestion': suggestion,
                    'baseline': {'stat_date': None, 'attempt_count': None, 'success_count': None,
                                 'rate': None, 'threshold': None, 'previous_period_rate': None,
                                 'gap_pp': None, 'decline_pp': None},
                    'diagnosis': {
                        'summary': '指标定义已由人工采纳并自动建立 Tracking 草稿；基线、数值目标、最小样本量和观察窗口尚未冻结，平台不会从自然语言中猜测数值。',
                        'routing': '待配置验证计划',
                        'method': 'accepted_metric_linkage',
                        'missing_information': [
                            '可计算的数值目标（0–1）', '最小有效样本量', '最少观察天数',
                            '可拉取的本地 JSON/CSV 数据源或 BI 接口字段映射',
                        ],
                    },
                    'owner': '', 'notes': '', 'metric_card': None, 'release': None,
                    'observations': [], 'retrospectives': [],
                    'history': [{'action': 'created_from_metric_card', 'at': now,
                                 'note': '指标定义采纳后自动创建；未猜测基线、目标或责任人。',
                                 'metric_run_id': lineage['metric_run_id']}],
                    'source': {'requirement_title': requirement_title,
                               'metric_suggestion': copy.deepcopy(suggestion)},
                }
                items.append(item)
                created.append(copy.deepcopy(item))
            self._write(items)
        return created

    def start_retrospective(self, item_id):
        with self.lock:
            items = self._read()
            item = next((x for x in items if x['id'] == item_id), None)
            if not item or not item['observations']:
                raise TrackingError('先录入模拟上线后的观察结果，再运行复盘 Skill')
            if item['status'] == 'closed':
                raise TrackingError('已闭环记录只读')
            runs = item.setdefault('retrospectives', [])
            if any(r['status'] in ('queued', 'running') for r in runs):
                raise TrackingError('已有复盘运行中，请等待结果')
            snapshot = copy.deepcopy({key: item.get(key) for key in (
                'id', 'title', 'source_id', 'product_line', 'business_scope', 'is_simulation',
                'baseline', 'diagnosis', 'owner', 'metric_card', 'release', 'observations')})
            snapshot['investigation_notes'] = copy.deepcopy([
                {'at': event['at'], 'note': event['note'], 'owner': event.get('owner', '')}
                for event in item['history'] if event['action'] == 'assign' and event.get('note')])
            run = {'id': uuid.uuid4().hex, 'status': 'running', 'created_at': _now(),
                'observation_id': item['observations'][-1]['id'],
                'metric_card_version': item['metric_card']['version'], 'input_snapshot': snapshot,
                'input_hash': hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
            runs.append(run)
            self._write(items)
            return copy.deepcopy(run), snapshot

    @staticmethod
    def _append_observation(item, body, moment, ingestion_mode='manual'):
        card, release = item['metric_card'], item['release']
        if not card or not release:
            raise TrackingError('先冻结指标并记录模拟上线，再录入验证数据')
        day = _date(body.get('stat_date'), '观察截止日期')
        days = _integer(body.get('window_days'), '实际观察天数')
        if (date.fromisoformat(day) - date.fromisoformat(release['date'])).days + 1 < days:
            raise TrackingError('观察窗口超出模拟上线后可用时间')
        if item['observations'] and day < item['observations'][-1]['stat_date']:
            raise TrackingError('新观察日期不能早于已有观察记录')
        attempt, success, rate = _counts(body.get('attempt_count'), body.get('success_count'))
        definition = _text(body.get('definition'), '实际指标口径')
        version = _text(body.get('version'), '实际验证版本', 200)
        note = body.get('note', '')
        if not isinstance(note, str) or len(note) > 10000:
            raise TrackingError('备注格式错误或过长')
        reasons = []
        if definition != card['definition']:
            reasons.append('实际口径与冻结口径不一致')
        if version != release['version']:
            reasons.append('实际版本与模拟上线版本不一致')
        if attempt < card['min_sample']:
            reasons.append('样本量不足')
        if days < card['observation_days']:
            reasons.append('观察时间不足')
        status = 'inconclusive' if reasons else ('met' if rate >= card['target_rate'] else 'not_met')
        observation = {'id': uuid.uuid4().hex, 'at': moment, 'stat_date': day,
            'attempt_count': attempt, 'success_count': success, 'rate': rate,
            'window_days': days, 'definition': definition, 'version': version,
            'metric_card_version': card['version'], 'target_rate': card['target_rate'],
            'status': status, 'reasons': reasons, 'note': note, 'is_simulation': True,
            'ingestion_mode': ingestion_mode,
            'recommendation': {'met': 'achieved', 'not_met': 'iterate', 'inconclusive': 'observe'}[status]}
        if body.get('source_record_id'):
            observation['source_record_id'] = str(body['source_record_id'])
        item['observations'].append(observation)
        item['status'] = 'awaiting_review'
        return observation

    def pull(self, item_id, scheduled=False):
        with self.lock:
            items = self._read()
            item = next((x for x in items if x['id'] == item_id), None)
            if not item:
                raise TrackingError('找不到该 Tracking 记录')
            if item['status'] == 'closed':
                raise TrackingError('已闭环记录只读')
            config = item.get('source_config')
            if not config:
                raise TrackingError('请先配置 JSON/CSV 模拟数据源')
            now = _now()
            run = {'id': uuid.uuid4().hex, 'started_at': now, 'trigger': 'schedule' if scheduled else 'manual',
                   'status': 'running', 'rows_seen': len(config['records']), 'imported': 0, 'skipped': 0}
            item.setdefault('pull_runs', []).append(run)
            candidate = copy.deepcopy(item)
            try:
                processed = set(candidate['source_config'].get('processed_record_ids', []))
                for row in candidate['source_config']['records']:
                    if row['source_record_id'] in processed:
                        run['skipped'] += 1
                        continue
                    self._append_observation(candidate, row, now, 'scheduled_pull' if scheduled else 'manual_pull')
                    processed.add(row['source_record_id'])
                    run['imported'] += 1
                candidate['source_config']['processed_record_ids'] = sorted(processed)
                candidate['source_config']['last_successful_pull_at'] = now
                if candidate['source_config']['schedule'] != 'manual':
                    candidate['source_config']['next_pull_at'] = _next_pull(candidate['source_config']['schedule'])
                run.update(status='completed', finished_at=_now())
                candidate['pull_runs'][-1] = run
                candidate['history'].append({'action': 'pull', 'at': now, 'note':
                    f"{'计划' if scheduled else '手动'}拉取完成：新增 {run['imported']} 条，跳过 {run['skipped']} 条。",
                    'pull_run_id': run['id']})
                candidate['updated_at'] = now
                items[items.index(item)] = candidate
                self._write(items)
                return copy.deepcopy(candidate)
            except TrackingError as exc:
                run.update(status='failed', finished_at=_now(), error=str(exc)[:1000])
                item['pull_runs'][-1] = run
                item['history'].append({'action': 'pull_failed', 'at': now, 'note': str(exc)[:1000],
                                        'pull_run_id': run['id']})
                item['updated_at'] = now
                self._write(items)
                raise

    def pull_due(self, moment=None):
        moment = moment or datetime.now(timezone.utc)
        due = []
        for item in self.list():
            config = item.get('source_config') or {}
            next_at = config.get('next_pull_at')
            if (item.get('status') != 'closed' and config.get('schedule') in ('daily', 'weekly')
                    and next_at and datetime.fromisoformat(next_at) <= moment):
                due.append(item['id'])
        results = []
        for item_id in due:
            try:
                results.append({'id': item_id, 'status': 'completed', 'item': self.pull(item_id, scheduled=True)})
            except TrackingError as exc:
                results.append({'id': item_id, 'status': 'failed', 'error': str(exc)})
        return results

    def finish_retrospective(self, item_id, run_id, output=None, error=None):
        with self.lock:
            items = self._read()
            item = next((x for x in items if x['id'] == item_id), None)
            run = next((r for r in (item or {}).get('retrospectives', []) if r['id'] == run_id), None)
            if not run or run['status'] not in ('queued', 'running'):
                raise TrackingError('复盘运行不存在或已完成，不可覆盖')
            run['finished_at'] = _now()
            if error:
                run.update(status='failed', error=str(error)[:2000])
            elif not isinstance(output, dict) or not isinstance(output.get('result'), dict) or not isinstance(output.get('audit'), dict):
                run.update(status='failed', error='复盘输出或审计记录不完整')
            else:
                run.update(status='completed', result=copy.deepcopy(output['result']), audit=copy.deepcopy(output['audit']))
            self._write(items)
            return copy.deepcopy(run)

    def record_writeback(self, item_id, result):
        with self.lock:
            items = self._read()
            item = next((x for x in items if x['id'] == item_id), None)
            if not item or not item.get('closure'):
                raise TrackingError('找不到可记录回流结果的闭环决定')
            item['closure']['writeback'] = copy.deepcopy(result)
            item['history'].append({'action': 'requirement_writeback', 'at': _now(),
                'note': '闭环决定已回写需求链路。' if result.get('status') in ('applied', 'skipped')
                        else '闭环决定回写受阻，可根据错误修复。', 'result': copy.deepcopy(result)})
            self._write(items)
            return copy.deepcopy(item)

    def import_payload(self, payload):
        if not isinstance(payload, dict):
            raise TrackingError('模拟 BI 材料必须是 JSON 对象')
        try:
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise TrackingError('JSON 含非法数值或类型') from None
        if len(encoded.encode('utf-8')) > 1024 * 1024:
            raise TrackingError('模拟 BI 材料不能超过 1 MB')
        form = payload.get('form')
        if not isinstance(form, dict) or not isinstance(form.get('data'), list) or len(form['data']) != 1:
            raise TrackingError('首版每次只接受 form.data 中一条指标记录')
        row = form['data'][0]
        if not isinstance(row, dict) or row.get('is_simulation') is not True:
            raise TrackingError('当前仅接受明确标记 is_simulation=true 的模拟数据')
        params = form.get('params')
        if not isinstance(params, list):
            raise TrackingError('缺少 form.params 来源信息')
        fields = {}
        for param in params:
            if not isinstance(param, dict) or not isinstance(param.get('name'), str):
                raise TrackingError('params 格式错误')
            if param['name'] in fields:
                raise TrackingError('params 名称不能重复')
            fields[param['name']] = param.get('value')
        if fields.get('source_type') != 'bi_alert':
            raise TrackingError('source_type 必须为 bi_alert')
        source_id = _text(fields.get('source_id'), '来源 ID', 200)
        product = _text(row.get('product_line'), '产品线', 100)
        scope = _text(row.get('business_scope'), '业务范围', 100)
        metric = _text(row.get('metric_name'), '指标名称', 100)
        if metric != '建图成功率':
            raise TrackingError('首版仅支持建图成功率，其他指标需要单独确认分子分母')
        day = _date(row.get('stat_date'), '统计日期')
        attempt, success, rate = _counts(row.get('mapping_attempt_count'), row.get('mapping_success_count'))
        supplied = _rate(row.get('mapping_success_rate'), '上报成功率')
        if abs(supplied - rate) > .000001:
            raise TrackingError('上报成功率与成功次数÷有效尝试数不一致')
        target = _rate(row.get('threshold'), '告警阈值')
        previous = _rate(row.get('previous_period_rate'), '上一周期成功率')
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock:
            items = self._read()
            existing = next((x for x in items if x['source_id'] == source_id), None)
            if existing:
                if existing['payload_hash'] != digest:
                    raise TrackingError('同来源 ID 已存在不同内容，请使用新来源 ID；原始数据未覆盖')
                return copy.deepcopy(existing)
            now = _now()
            item = {'id': uuid.uuid4().hex, 'title': f'{scope} · {metric}异常核查',
                'source_id': source_id, 'product_line': product, 'business_scope': scope,
                'is_simulation': True, 'status': 'pending_assignment', 'created_at': now,
                'baseline': {'stat_date': day, 'attempt_count': attempt, 'success_count': success,
                    'rate': rate, 'threshold': target, 'previous_period_rate': previous,
                    'gap_pp': round((target-rate)*100, 6), 'decline_pp': round((previous-rate)*100, 6)},
                'diagnosis': {'summary': f'上报数值一致：{success}/{attempt}={rate:.1%}；' + ('低于阈值，数值告警触发。' if rate < target else '未低于阈值，数值告警未触发。') + '统计去重和数据完整性仍需核查；不据此认定 App 根因或开发需求。',
                    'routing': '待人工分配', 'method': 'deterministic_validation',
                    'missing_information': ['mapping_attempt_id 去重与有效尝试口径的原始证据', '失败阶段、错误码与失败日志', '设备型号、固件和 App 版本分布', '统计窗口起止时间与上一周期可比性', '跨设备、算法、网络及 App 的责任核查结果']},
                'owner': '', 'notes': '', 'metric_card': None, 'release': None,
                'observations': [], 'retrospectives': [], 'history': [{'action': 'import', 'at': now, 'note': '手动导入模拟 BI API 材料；未自动派人或创建需求'}],
                'raw_payload': copy.deepcopy(payload), 'payload_hash': digest}
            items.append(item)
            self._write(items)
            return copy.deepcopy(item)

    def update(self, body):
        if not isinstance(body, dict):
            raise TrackingError('更新内容必须是对象')
        with self.lock:
            items = self._read()
            item = next((x for x in items if x['id'] == body.get('id')), None)
            if item is None:
                raise TrackingError('找不到该 Tracking 记录')
            if item['status'] == 'closed':
                raise TrackingError('已闭环记录只读；新一轮请建立新来源记录')
            action = body.get('action')
            now = _now()
            note = body.get('note', '')
            if not isinstance(note, str) or len(note) > 10000:
                raise TrackingError('备注格式错误或过长')
            event = {'action': action, 'at': now, 'note': note}
            if action == 'assign':
                owner = body.get('owner', '')
                if not isinstance(owner, str) or len(owner) > 200:
                    raise TrackingError('核查负责人格式无效')
                if owner.strip():
                    item['owner'] = owner.strip()
                    item['diagnosis']['routing'] = '人工指定：' + item['owner']
                    if item['status'] == 'pending_assignment':
                        item['status'] = 'investigating'
                elif not note.strip():
                    raise TrackingError('至少填写核查说明；负责人不明确时可保持待人工分配')
                event['owner'] = item['owner']
            elif action == 'freeze':
                if item['release']:
                    raise TrackingError('上线后不可修改冻结指标；需新一轮验证')
                card = {'version': (item['metric_card'] or {}).get('version', 0) + 1,
                    'target_rate': _rate(body.get('target_rate'), '验证目标'),
                    'min_sample': _integer(body.get('min_sample'), '最小样本量'),
                    'observation_days': _integer(body.get('observation_days'), '观察天数'),
                    'definition': _text(body.get('definition'), '指标口径'),
                    'operator': '>=', 'unit': 'ratio', 'confirmed_at': now,
                    'success_rule': 'all_confirmed_metrics_met', 'is_simulation': True}
                item['metric_card'] = card
                item['status'] = 'metric_frozen'
                event['metric_card'] = copy.deepcopy(card)
            elif action == 'configure_source':
                source_format = body.get('source_format')
                schedule = body.get('schedule')
                if schedule not in ('manual', 'daily', 'weekly'):
                    raise TrackingError('拉取计划必须是 manual、daily 或 weekly')
                records = _source_records(body.get('content'), source_format)
                previous = item.get('source_config') or {}
                item['source_config'] = {'type': 'embedded_file', 'format': source_format,
                    'schedule': schedule, 'records': records,
                    'processed_record_ids': previous.get('processed_record_ids', []),
                    'configured_at': now,
                    'next_pull_at': now if schedule != 'manual' and item.get('release') else None,
                    'last_successful_pull_at': previous.get('last_successful_pull_at')}
                item.setdefault('pull_runs', [])
                event.update(source_format=source_format, schedule=schedule, rows=len(records))
            elif action == 'release':
                if not item['metric_card'] or not item['owner']:
                    raise TrackingError('先人工指定核查负责人并冻结验证目标')
                if item['release']:
                    raise TrackingError('已有上线记录，不覆盖历史；新版本请新建验证轮次')
                day = _date(body.get('date'), '模拟上线日期')
                if item['baseline'].get('stat_date') and day < item['baseline']['stat_date']:
                    raise TrackingError('模拟上线日期不能早于告警统计日期')
                item['release'] = {'version': _text(body.get('version'), '模拟上线版本', 200),
                    'date': day, 'note': note, 'metric_card_version': item['metric_card']['version'], 'is_simulation': True}
                if item.get('source_config', {}).get('schedule') in ('daily', 'weekly'):
                    item['source_config']['next_pull_at'] = now
                item['status'] = 'tracking'
                event['release'] = copy.deepcopy(item['release'])
            elif action == 'observe':
                observation = self._append_observation(item, body, now)
                event['observation_id'] = observation['id']
            elif action == 'close':
                decision = body.get('decision')
                if decision not in ('achieved', 'iterate', 'observe'):
                    raise TrackingError('闭环决定必须是 achieved、iterate 或 observe')
                if not item['observations']:
                    raise TrackingError('尚无上线后验证结果，不能作出复盘决定')
                current_retrospective = next((run for run in reversed(item.get('retrospectives', []))
                    if run.get('status') == 'completed'
                    and run.get('observation_id') == item['observations'][-1]['id']), None)
                if not current_retrospective:
                    raise TrackingError('先完成当前观察结果的复盘 Skill，再作出闭环决定')
                if decision == 'achieved' and item['observations'][-1]['status'] != 'met':
                    raise TrackingError('最新验证结果尚未达标，不能确认达成')
                _text(note, '人工复盘说明')
                item['closure'] = {'decision': decision, 'note': note, 'at': now,
                    'observation_id': item['observations'][-1]['id'],
                    'retrospective_id': current_retrospective['id'], 'is_simulation': True}
                item['status'] = 'closed' if decision == 'achieved' else 'tracking'
                event['decision'] = decision
            else:
                raise TrackingError('未知 Tracking 操作')
            item['notes'] = note
            item['updated_at'] = now
            item['history'].append(event)
            self._write(items)
            return copy.deepcopy(item)
