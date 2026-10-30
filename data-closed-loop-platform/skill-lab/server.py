#!/usr/bin/env python3
"""Isolated, local-only Skill evaluation workbench. No business-system synchronization."""
import argparse
import base64
import hashlib
import json
import mimetypes
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
MAX_BODY = 15 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


class LabError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def string(value, name, limit=100000, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise LabError(f'{name} 格式无效')
    return value.strip()


class Store:
    def __init__(self, path, executor=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.executor = executor
        self.lock = threading.RLock()
        with self.db() as db:
            for table in ('cases', 'runs', 'pool'):
                db.execute(f'CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            for ident, raw in db.execute('SELECT id,data FROM runs').fetchall():
                run = json.loads(raw)
                if run['status'] in ('queued', 'running'):
                    run.update(status='failed', error='服务重启中断运行；可重新运行，原记录保留。', finished_at=now())
                    self.save(db, 'runs', run)
                elif (run['status'] == 'completed' and run.get('stage') == 'analysis'
                      and run.get('review', {}).get('decision') != 'reject'):
                    # Upgrade successful history, without inventing human approval.
                    try:
                        self._analysis_candidates(run)
                    except LabError as exc:
                        run['pool_publication'] = {'mode': 'automatic', 'status': 'blocked',
                                                   'count': 0, 'error': str(exc)}
                    else:
                        self._publish_analysis(db, run)
                    self.save(db, 'runs', run)
        self.path.chmod(0o600)

    def db(self):
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def save(db, table, value):
        db.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?)', (value['id'], json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def get(db, table, ident):
        if not isinstance(ident, str):
            raise LabError('记录编号无效')
        row = db.execute(f'SELECT data FROM {table} WHERE id=?', (ident,)).fetchone()
        if not row:
            raise LabError('记录不存在', 404)
        return json.loads(row[0])

    @staticmethod
    def _analysis_candidates(run):
        result = run.get('result')
        candidates = result.get('candidates') if isinstance(result, dict) else None
        if (not isinstance(candidates, list) or any(
                not isinstance(candidate, dict) or not isinstance(candidate.get('title'), str)
                or not candidate['title'].strip() for candidate in candidates)):
            raise LabError('候选需求结构无效，未写入需求池', 502)
        return candidates

    def _publish_analysis(self, db, run):
        """Publish in the run-completion transaction, idempotent per run/index."""
        candidates = self._analysis_candidates(run)
        published_at = run.get('pool_publication', {}).get('published_at') or now()
        for index, candidate in enumerate(candidates):
            ident = f"{run['id']}-{index}"
            if db.execute('SELECT 1 FROM pool WHERE id=?', (ident,)).fetchone():
                continue
            item = dict(candidate)
            item.update(id=ident, case_id=run['case_id'], run_id=run['id'],
                        candidate=candidate, corrected_text='', correction_scope='item',
                        status='pending_definition', entry_mode='automatic', created_at=published_at)
            self.save(db, 'pool', item)
        run['pool_publication'] = {'mode': 'automatic', 'status': 'published',
                                   'count': len(candidates), 'published_at': published_at}

    def state(self):
        with self.db() as db:
            result = {table: [json.loads(r[0]) for r in db.execute(f'SELECT data FROM {table} ORDER BY rowid DESC')]
                      for table in ('cases', 'runs', 'pool')}
        # Avoid repeatedly returning base64 payloads in polling responses.
        for case in result['cases']:
            for index, attachment in enumerate(case['attachments']):
                attachment.pop('data', None)
                attachment['url'] = f"/api/attachments/{case['id']}/{index}"
        for run in result['runs']:
            run.pop('input_snapshot', None)
            if run.get('stage') in ('definition', 'metrics') and not run.get('pool_item_id'):
                run['legacy_scope_warning'] = '旧版结果未绑定单条需求，仅供历史追溯，请从需求池重新生成。'
        # A logical stage projection, not a destructive move: retain original pool
        # rows and all run history so source evidence and links remain stable.
        definitions = {}
        for run in sorted(result['runs'], key=lambda r: (r.get('finished_at', ''), r.get('created_at', '')), reverse=True):
            if (run.get('stage') == 'definition' and run.get('status') == 'completed'
                    and run.get('pool_item_id') and isinstance(run.get('result', {}).get('definition'), dict)
                    and run['result']['definition']):
                definitions.setdefault(run['pool_item_id'], run)
        result['requirements'] = []
        for item in result['pool']:
            definition = definitions.get(item['id'])
            if not definition or definition.get('case_id') != item['case_id']:
                continue
            result['requirements'].append({**item, 'definition_run_id': definition['id'],
                'definition_review_status': definition.get('review', {}).get('decision', 'pending'),
                'definition_completed_at': definition.get('finished_at', definition['created_at'])})
        return result

    def create_case(self, payload):
        if set(payload) - {'title', 'kind', 'product_line', 'text', 'attachments'}:
            raise LabError('案例含未知字段')
        kind = payload.get('kind', 'auto')
        if kind not in ('auto', 'problem', 'demand', 'voc', 'bi'):
            raise LabError('分析模式无效')
        attachments = payload.get('attachments', [])
        if not isinstance(attachments, list) or len(attachments) > 8:
            raise LabError('最多上传 8 个附件')
        cleaned = []
        total = 0
        for attachment in attachments:
            if not isinstance(attachment, dict) or set(attachment) != {'name', 'mime', 'data'}:
                raise LabError('附件格式无效')
            name = string(attachment['name'], '文件名', 240)
            mime = string(attachment['mime'], '附件类型', 120)
            try:
                raw = base64.b64decode(attachment['data'], validate=True)
            except (ValueError, TypeError):
                raise LabError('附件编码无效')
            total += len(raw)
            if len(raw) > 5 * 1024 * 1024:
                raise LabError('单个附件不能超过 5 MB', 413)
            if total > 10 * 1024 * 1024 or not raw:
                raise LabError('附件总大小需在 10 MB 内且不能为空')
            entry = dict(name=name, mime=mime, data=attachment['data'], size=len(raw),
                         sha256=hashlib.sha256(raw).hexdigest())
            from document_bridge import EXTENSIONS, extract
            if Path(name).suffix.lower() in EXTENSIONS:
                try:
                    entry['parsed'] = extract(name, mime, raw)
                except RuntimeError as exc:
                    raise LabError(str(exc), 422) from exc
            cleaned.append(entry)
        text = string(payload.get('text', ''), '原始材料', required=False)
        if not text and not cleaned:
            raise LabError('请填写原始材料或上传附件')
        case = dict(id=uuid.uuid4().hex, title=string(payload.get('title'), '标题', 200), kind=kind,
                    product_line=string(payload.get('product_line'), '产品线', 100), text=text,
                    attachments=cleaned, created_at=now())
        with self.lock, self.db() as db:
            self.save(db, 'cases', case)
        return {**case, 'attachments': [{k: v for k, v in a.items() if k != 'data'} for a in cleaned]}

    def start_run(self, payload):
        if set(payload) - {'case_id', 'stage', 'parent_run_id', 'pool_item_id'}:
            raise LabError('运行含未知字段')
        stage = payload.get('stage', 'analysis')
        if stage not in ('analysis', 'definition', 'metrics'):
            raise LabError('运行阶段无效')
        with self.lock, self.db() as db:
            case = self.get(db, 'cases', payload.get('case_id'))
            parent = None
            item = None
            selected_requirement = None
            scoped_case = case
            if stage != 'analysis':
                if not payload.get('pool_item_id'):
                    raise LabError('请从需求池选择具体需求后生成，不能对整批候选直接生成定义', 409)
                item = self.get(db, 'pool', payload['pool_item_id'])
                if item['case_id'] != case['id']:
                    raise LabError('需求与案例不一致', 409)
                parent = self.get(db, 'runs', payload.get('parent_run_id'))
                expected = 'analysis' if stage == 'definition' else 'definition'
                automatic_analysis = (stage == 'definition' and
                    parent.get('pool_publication', {}).get('status') == 'published')
                if (parent['case_id'] != case['id'] or parent['stage'] != expected
                        or parent['status'] != 'completed'
                        or (not automatic_analysis and parent.get('review', {}).get('decision') != 'accept')):
                    raise LabError('需先完成同一案例的上游分析；指标定义仍需采纳对应需求定义', 409)
                if stage == 'definition' and parent['id'] != item['run_id']:
                    raise LabError('该分析运行不是当前需求的来源', 409)
                if stage == 'metrics' and parent.get('pool_item_id') != item['id']:
                    raise LabError('指标定义必须基于当前需求已采纳的定义，不能引用其他需求或旧版整批定义', 409)
                selected_requirement = dict(item.get('candidate') or {})
                if not selected_requirement:
                    raise LabError('需求缺少原始候选内容', 409)
                # Old pool rows copied one batch-wide correction into every candidate.
                # Such text is not evidence that a particular candidate was corrected.
                correction_scoped = item.get('correction_scope') == 'item'
                if stage == 'definition' and len(parent['result'].get('candidates', [])) == 1:
                    correction_scoped = True
                if correction_scoped and item.get('corrected_text'):
                    selected_requirement['rough_requirement'] = item['corrected_text']
                scoped_case = dict(id=case['id'], title=selected_requirement.get('title', item['id']),
                    kind=case['kind'], product_line=case['product_line'],
                    text=json.dumps({'selected_requirement': selected_requirement}, ensure_ascii=False),
                    attachments=[])
                for (raw,) in db.execute('SELECT data FROM runs'):
                    active = json.loads(raw)
                    if active.get('pool_item_id') == item['id'] and active['stage'] == stage and active['status'] in ('queued', 'running'):
                        return {k: v for k, v in active.items() if k != 'input_snapshot'}
            elif payload.get('pool_item_id') or payload.get('parent_run_id'):
                raise LabError('原始材料分析不能指定需求池条目或上游运行')
            run = dict(id=uuid.uuid4().hex, case_id=case['id'], stage=stage, parent_run_id=payload.get('parent_run_id'),
                       status='queued', created_at=now(), input_snapshot=scoped_case,
                       input_sha256=hashlib.sha256(json.dumps(scoped_case, sort_keys=True).encode()).hexdigest())
            if item:
                run.update(pool_item_id=item['id'], requirement_title=selected_requirement.get('title', item['id']),
                           selected_requirement=selected_requirement)
            self.save(db, 'runs', run)
        threading.Thread(target=self._execute, args=(run, scoped_case, parent), daemon=True).start()
        return {k: v for k, v in run.items() if k != 'input_snapshot'}

    def run_pool_batch(self, payload):
        if set(payload) != {'pool_item_ids', 'stage'} or payload.get('stage') not in ('definition', 'metrics'):
            raise LabError('批量处理字段无效')
        ids = payload['pool_item_ids']
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise LabError('请选择 1–20 条不同的需求')
        runs, errors = [], []
        for item_id in ids:
            try:
                with self.db() as db:
                    item = self.get(db, 'pool', item_id)
                    parent_id = item['run_id']
                    if payload['stage'] == 'metrics':
                        parents = [json.loads(row[0]) for row in db.execute('SELECT data FROM runs ORDER BY rowid DESC')]
                        parent = next((r for r in parents if r.get('pool_item_id') == item_id and r['stage'] == 'definition'
                            and r['status'] == 'completed' and r.get('review', {}).get('decision') == 'accept'), None)
                        if not parent:
                            raise LabError('需先完成并采纳该需求的定义', 409)
                        parent_id = parent['id']
                runs.append(self.start_run({'case_id': item['case_id'], 'stage': payload['stage'],
                    'pool_item_id': item_id, 'parent_run_id': parent_id}))
            except LabError as exc:
                errors.append({'pool_item_id': item_id, 'error': str(exc), 'status': exc.status})
        return {'runs': runs, 'errors': errors}

    def _execute(self, run, case, parent):
        run.update(status='running', started_at=now())
        with self.db() as db:
            self.save(db, 'runs', run)
        try:
            executor = self.executor
            if executor is None:
                from runner import execute
                executor = execute
            parent_result = None
            if parent:
                if run['stage'] == 'definition':
                    parent_result = {'selected_requirement': run['selected_requirement'],
                                     'pool_item_id': run['pool_item_id']}
                else:
                    parent_result = dict(parent['result'])
                    parent_result['selected_requirement'] = run['selected_requirement']
                    parent_result['human_review'] = parent['review']
            output = executor(case, run['stage'], parent_result)
            if not isinstance(output, dict) or not isinstance(output.get('result'), dict) or not isinstance(output.get('audit'), dict):
                raise LabError('运行器未返回有效结果与审计', 502)
            run.update(status='completed', result=output['result'], audit=output['audit'])
            run['finished_at'] = now()
            with self.lock, self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if run['stage'] == 'analysis':
                    self._publish_analysis(db, run)
                self.save(db, 'runs', run)
            return
        except Exception as exc:
            # Runtime errors are actionable, but credentials/request bodies must not leak.
            message = str(exc) if isinstance(exc, (LabError, ValueError, RuntimeError)) else '分析失败，请检查模型配置或运行日志后重试。'
            run.update(status='failed', error=message[:600])
        run['finished_at'] = now()
        with self.db() as db:
            self.save(db, 'runs', run)

    def review(self, payload):
        if set(payload) - {'run_id', 'decision', 'note', 'corrected_text'}:
            raise LabError('评价含未知字段')
        decision = payload.get('decision')
        if decision not in ('accept', 'reject'):
            raise LabError('评价决定无效')
        note = string(payload.get('note', ''), '评价备注', 5000, False)
        corrected = string(payload.get('corrected_text', ''), '修正结果', 30000, False)
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self.get(db, 'runs', payload.get('run_id'))
            if run['status'] != 'completed' or run.get('review'):
                raise LabError('只可评价尚未评价的成功运行；如需修改请重新运行', 409)
            run['review'] = dict(decision=decision, note=note, corrected_text=corrected, at=now())
            self.save(db, 'runs', run)
            if decision == 'accept' and run['stage'] == 'analysis':
                candidates = self._analysis_candidates(run)
                for index, candidate in enumerate(candidates):
                    # Optional evaluation may add corrections, never reimport or
                    # overwrite source evidence and original publication metadata.
                    item = self.get(db, 'pool', f"{run['id']}-{index}")
                    item.update(corrected_text=corrected,
                                correction_scope='item' if len(candidates) == 1 else 'batch')
                    self.save(db, 'pool', item)
        return run

    def apply_tracking_decision(self, tracking_item):
        lineage = tracking_item.get('lineage') or {}
        requirement_id = lineage.get('requirement_id')
        closure = tracking_item.get('closure') or {}
        decision = closure.get('decision')
        if not requirement_id:
            return {'status': 'skipped', 'reason': '该记录来自独立 BI 告警，没有关联需求'}
        if decision not in ('achieved', 'iterate', 'observe'):
            raise LabError('Tracking 尚无可回写的人工闭环决定', 409)
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            requirement = self.get(db, 'pool', requirement_id)
            requirement['tracking_summary'] = {'tracking_id': tracking_item['id'],
                'decision': decision, 'observation_id': closure.get('observation_id'),
                'retrospective_id': closure.get('retrospective_id'), 'at': closure.get('at')}
            requirement['lifecycle_status'] = {'achieved': 'closed', 'iterate': 'iteration_required',
                                               'observe': 'observing'}[decision]
            self.save(db, 'pool', requirement)
            result = {'status': 'applied', 'requirement_id': requirement_id,
                      'lifecycle_status': requirement['lifecycle_status']}
            if decision == 'iterate':
                retrospective = next((run for run in tracking_item.get('retrospectives', [])
                    if run.get('id') == closure.get('retrospective_id')), {})
                next_action = (retrospective.get('result') or {}).get('next_action') or closure.get('note')
                iteration_id = f"iteration-{tracking_item['id']}-{closure['observation_id']}"
                exists = db.execute('SELECT 1 FROM pool WHERE id=?', (iteration_id,)).fetchone()
                if not exists:
                    source_candidate = dict(requirement.get('candidate') or {})
                    source_title = source_candidate.get('title') or requirement.get('title') or '原需求'
                    source_candidate.update(
                        title=f'{source_title} · 复盘迭代',
                        problem_statement='上线后观测与冻结目标未形成可关闭结论，需要基于复盘结果继续迭代。',
                        rough_requirement=next_action,
                        evidence=(source_candidate.get('evidence', '') + '\n' +
                                  f"Tracking {tracking_item['id']}：{closure.get('note', '')}").strip())
                    iteration = {'id': iteration_id, 'case_id': requirement['case_id'],
                        'run_id': requirement['run_id'], 'candidate': source_candidate,
                        **source_candidate, 'corrected_text': '', 'correction_scope': 'item',
                        'status': 'pending_definition', 'entry_mode': 'retrospective_iteration',
                        'parent_requirement_id': requirement_id, 'tracking_id': tracking_item['id'],
                        'retrospective_id': closure.get('retrospective_id'), 'created_at': now()}
                    self.save(db, 'pool', iteration)
                result['iteration_requirement_id'] = iteration_id
            return result


def create_server(port=8086, path=None, executor=None):
    store = Store(path or ROOT.parent / '.runtime' / 'skill-lab' / 'lab.sqlite3', executor)
    from tracking import TrackingStore, sample_payload
    tracking = TrackingStore(store.path.with_name('tracking.json'))

    def state():
        value = store.state()
        value['tracking'] = [{key: item.get(key) for key in
            ('id', 'title', 'status', 'source_kind', 'lineage', 'metric_suggestion')}
            for item in tracking.list()]
        return value

    def publish_metrics(run_id):
        try:
            with store.db() as db:
                current = store.get(db, 'runs', run_id)
                definition = store.get(db, 'runs', current['parent_run_id'])
                item = store.get(db, 'pool', current['pool_item_id'])
                case = store.get(db, 'cases', current['case_id'])
            if current.get('tracking_publication', {}).get('status') == 'published':
                return current
            context = {
                'product_line': case['product_line'],
                'requirement_title': current.get('requirement_title') or item.get('title') or item['id'],
                'feature': (item.get('candidate') or {}).get('feature', ''),
                'metrics': current.get('result', {}).get('metrics'),
                'lineage': {
                    'source_record_id': case['id'],
                    'analysis_run_id': item['run_id'],
                    'candidate_id': item['id'],
                    'requirement_id': item['id'],
                    'definition_run_id': definition['id'],
                    'metric_run_id': current['id'],
                },
            }
            records = tracking.create_from_metric_run(context)
            publication = {'status': 'published', 'count': len(records),
                           'tracking_ids': [item['id'] for item in records], 'published_at': now()}
        except Exception as exc:
            publication = {'status': 'blocked', 'count': 0,
                           'error': str(exc)[:600], 'attempted_at': now()}
        with store.lock, store.db() as db:
            current = store.get(db, 'runs', run_id)
            current['tracking_publication'] = publication
            store.save(db, 'runs', current)
        return current

    def review(payload):
        run = store.review(payload)
        if run.get('stage') != 'metrics' or run.get('review', {}).get('decision') != 'accept':
            return run
        return publish_metrics(run['id'])

    def import_tracking(payload):
        return tracking.import_payload(payload.get('payload'))

    def retrospective(payload):
        run, snapshot = tracking.start_retrospective(payload.get('id'))

        def work():
            try:
                from tracking_runner import execute_retrospective
                output = execute_retrospective(snapshot)
                tracking.finish_retrospective(snapshot['id'], run['id'], output=output)
            except Exception as exc:
                message = str(exc) if isinstance(exc, RuntimeError) else '复盘运行失败；原始数据和人工决定未修改，可重试。'
                tracking.finish_retrospective(snapshot['id'], run['id'], error=message)
        threading.Thread(target=work, daemon=True).start()
        return run

    def pull_tracking(payload):
        return tracking.pull(payload.get('id'))

    def update_tracking(payload):
        item = tracking.update(payload)
        if payload.get('action') != 'close':
            return item
        try:
            writeback = store.apply_tracking_decision(item)
        except Exception as exc:
            writeback = {'status': 'blocked', 'error': str(exc)[:600]}
        return tracking.record_writeback(item['id'], writeback)

    # Idempotent migration: historical accepted metrics should enter the same
    # closed-loop path without requiring the user to rerun or re-accept them.
    for historical in store.state()['runs']:
        if (historical.get('stage') == 'metrics' and historical.get('status') == 'completed'
                and historical.get('review', {}).get('decision') == 'accept'):
            publish_metrics(historical['id'])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, write=False):
            host = f'127.0.0.1:{self.server.server_port}'
            return self.headers.get('Host') == host and self.headers.get('Origin') in (('http://' + host,) if write else (None, 'http://' + host))

        def do_GET(self):
            if not self.allowed():
                return self.send_json(403, {'error': '仅允许本机页面访问'})
            path = urlsplit(self.path).path
            if path == '/api/state':
                return self.send_json(200, state())
            if path == '/api/health':
                return self.send_json(200, {'ok': True, 'mode': 'isolated_skill_lab'})
            if path == '/api/tracking':
                return self.send_json(200, {'items': tracking.list()})
            if path == '/api/tracking/sample':
                return self.send_json(200, sample_payload())
            attachment_match = re.fullmatch(r'/api/attachments/([a-f0-9]{32})/(\d+)', path)
            if attachment_match:
                try:
                    with store.db() as db:
                        case = store.get(db, 'cases', attachment_match[1])
                    attachment = case['attachments'][int(attachment_match[2])]
                except (LabError, IndexError):
                    return self.send_json(404, {'error': '附件不存在'})
                body = base64.b64decode(attachment['data'])
                self.send_response(200)
                mime = attachment['mime'] if attachment['mime'] in ('image/png', 'image/jpeg', 'image/webp', 'image/gif') else 'application/octet-stream'
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
                self.end_headers()
                return self.wfile.write(body)
            name = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/tracking.js': 'tracking.js', '/styles.css': 'styles.css'}.get(path)
            if not name or not (ROOT / name).is_file():
                return self.send_json(404, {'error': '页面不存在'})
            body = (ROOT / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', (mimetypes.guess_type(name)[0] or 'text/plain') + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.allowed(write=True):
                return self.send_json(403, {'error': '仅允许本机页面操作'})
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    raise LabError('请求长度无效')
                length = int(self.headers['Content-Length'])
                if not 0 < length <= MAX_BODY:
                    raise LabError('请求超出 15 MB 限制', 413)
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise LabError('需要 JSON 请求', 415)
                payload = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(payload, dict):
                    raise LabError('请求须为对象')
                actions = {'/api/cases': store.create_case, '/api/run': store.start_run, '/api/review': review,
                           '/api/pool/run': store.run_pool_batch, '/api/tracking/import': import_tracking,
                           '/api/tracking/update': update_tracking, '/api/tracking/pull': pull_tracking,
                           '/api/tracking/retrospective': retrospective}
                action = actions.get(self.path)
                if not action:
                    raise LabError('接口不存在', 404)
                self.send_json(202 if self.path in ('/api/run', '/api/pool/run', '/api/tracking/retrospective') else 200, action(payload))
            except LabError as exc:
                self.send_json(exc.status, {'error': str(exc)})
            except ValueError as exc:
                self.send_json(400, {'error': str(exc) if self.path.startswith('/api/tracking/') else '请求格式无效'})
            except (TypeError, UnicodeError, RecursionError):
                self.send_json(400, {'error': '请求格式无效'})
            except Exception:
                self.send_json(500, {'error': '操作失败，未确认成功，请刷新检查状态'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.store = store
    server.tracking = tracking
    stop_scheduler = threading.Event()
    original_close = server.server_close
    def close_with_scheduler():
        stop_scheduler.set()
        original_close()
    server.server_close = close_with_scheduler
    def schedule_loop():
        while not stop_scheduler.wait(15):
            try:
                tracking.pull_due()
            except Exception:
                # Individual pull failures are persisted on their Tracking records;
                # a scheduler loop must stay alive without leaking raw source data.
                pass
    threading.Thread(target=schedule_loop, name='skill-lab-tracking-scheduler', daemon=True).start()
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8086)
    parser.add_argument('--store', type=Path)
    args = parser.parse_args()
    server = create_server(args.port, args.store)
    print(f'Skill 试验台：http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
