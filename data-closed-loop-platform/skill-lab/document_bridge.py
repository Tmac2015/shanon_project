"""Run bounded document extraction with an available local Python runtime."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

EXTENSIONS = {'.xlsx', '.xls', '.pdf', '.pptx', '.ppt', '.json'}


def extract(name, mime, raw):
    parser = Path(__file__).with_name('document_parser.py')
    executable = sys.executable
    if not all(importlib.util.find_spec(p) for p in ('openpyxl', 'pypdf', 'pptx')):
        bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'
        if bundled.is_file():
            executable = str(bundled)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    try:
        completed = subprocess.run([executable, str(parser)], input=json.dumps({
            'name': name, 'mime': mime, 'data': base64.b64encode(raw).decode()}),
            capture_output=True, text=True, timeout=90, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('附件解析无法启动或超时，请缩小文件后重试') from exc
    if completed.returncode:
        # Parser prints only curated errors, never file content or paths.
        raise RuntimeError(completed.stderr.strip()[:500] or '文件解析失败')
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError('文件解析器返回无效结果') from exc
    return result
