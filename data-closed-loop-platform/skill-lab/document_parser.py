"""Bounded, read-only attachment extraction. Never execute document content."""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

MAX_TEXT = 120000
MAX_UNITS = 100
MAX_ROWS = 10000


def _zip_check(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(x.file_size for x in entries) > 80 * 1024 * 1024:
            raise RuntimeError('文档解压规模超过安全限制，请拆分文件')
        if any(x.flag_bits & 1 for x in entries):
            raise RuntimeError('加密文档不能解析，请提供未加密副本')


def _dependency(name):
    try:
        return __import__(name)
    except ImportError as exc:
        raise RuntimeError('缺少文档解析依赖 ' + name + '，请安装 skill-lab/requirements.txt') from exc


def _json(raw):
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError('duplicate key')
            output[key] = value
        return output
    def invalid(_):
        raise ValueError('invalid number')
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, (dict, list)):
        raise RuntimeError('JSON 顶层须为对象或数组')
    return '[JSON 根路径 $]\n' + json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), [], 1


def _xlsx(raw):
    module = _dependency('openpyxl')
    books = []
    try:
        books = [module.load_workbook(io.BytesIO(raw), read_only=True, data_only=mode, keep_links=False)
                 for mode in (False, True)]
        if len(books[0].worksheets) > MAX_UNITS:
            raise RuntimeError('Excel 工作表超过 100 张，请拆分')
        lines, warnings, rows = [], [], 0
        for sheet, cached in zip(books[0].worksheets, books[1].worksheets):
            lines.append('[工作表：' + sheet.title + ']')
            if sheet.max_row and sheet.max_row > MAX_ROWS or sheet.max_column and sheet.max_column > 500:
                raise RuntimeError('Excel 行列范围过大，请清理空白格式或拆分文件')
            for row, values in zip(sheet.iter_rows(), cached.iter_rows()):
                cells = []
                for cell, value in zip(row, values):
                    if cell.value is None:
                        continue
                    text = str(cell.value)
                    if cell.data_type == 'f':
                        text += ' [公式缓存=' + (str(value.value) if value.value is not None else '缺失，未计算') + ']'
                    cells.append(cell.coordinate + '=' + text)
                if cells:
                    rows += 1
                    if rows > MAX_ROWS:
                        raise RuntimeError('Excel 总非空行超过 10000 行，请拆分')
                    lines.append('行 ' + str(row[0].row) + ': ' + ' | '.join(cells))
                    if sum(map(len, lines)) > MAX_TEXT:
                        raise RuntimeError('Excel 提取文本超过 120000 字，请拆分')
            if sheet.sheet_state != 'visible':
                warnings.append('包含隐藏工作表：' + sheet.title)
        warnings.append('仅提取单元格内容与公式缓存，不执行公式；图表、图片、批注及格式未作为视觉证据读取。')
        return '\n'.join(lines), warnings, len(books[0].worksheets)
    finally:
        for book in books:
            book.close()


def _pdf(raw):
    module = _dependency('pypdf')
    reader = module.PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise RuntimeError('PDF 已加密，请提供未加密副本')
    if len(reader.pages) > MAX_UNITS:
        raise RuntimeError('PDF 超过 100 页，请拆分')
    lines, warnings, meaningful = [], [], False
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ''
        meaningful = meaningful or bool(text.strip())
        if not text.strip():
            warnings.append(f'第 {index} 页无可提取文本，可能是扫描页，未执行 OCR。')
        lines.append(f'[PDF 第 {index} 页]\n{text}')
        if sum(map(len, lines)) > MAX_TEXT:
            raise RuntimeError('PDF 提取文本超过 120000 字，请拆分')
    if not meaningful:
        raise RuntimeError('PDF 无可提取文本；扫描型 PDF 需要 OCR 或改为上传截图')
    warnings.append('仅提取 PDF 文本层；图片、图表及版式未作为视觉证据读取。')
    return '\n\n'.join(lines), warnings, len(reader.pages)


def _pptx(raw):
    module = _dependency('pptx')
    deck = module.Presentation(io.BytesIO(raw))
    if len(deck.slides) > MAX_UNITS:
        raise RuntimeError('演示文稿超过 100 页，请拆分')
    def texts(shapes):
        parts = []
        for shape in shapes:
            if hasattr(shape, 'shapes'):
                parts.extend(texts(shape.shapes))
            if shape.has_text_frame and shape.text.strip():
                parts.append(shape.text)
            if shape.has_table:
                for i, row in enumerate(shape.table.rows, 1):
                    parts.append('表格行 ' + str(i) + ': ' + ' | '.join(c.text for c in row.cells))
        return parts
    lines = []
    for index, slide in enumerate(deck.slides, 1):
        parts = texts(slide.shapes)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes and notes.text.strip():
                parts.append('[演讲备注]\n' + notes.text)
        lines.append(f'[幻灯片 {index}]\n' + '\n'.join(parts))
        if sum(map(len, lines)) > MAX_TEXT:
            raise RuntimeError('PPT 提取文本超过 120000 字，请拆分')
    if not any(texts(s.shapes) for s in deck.slides):
        raise RuntimeError('PPT 无可提取正文，可能仅包含图片，请上传截图')
    return '\n\n'.join(lines), ['仅提取文本、表格与备注；图片、图表、动画及布局未作为视觉证据读取。'], len(deck.slides)


def _legacy(raw, extension):
    executable = shutil.which('soffice') or shutil.which('libreoffice')
    if not executable:
        raise RuntimeError('旧版 ' + extension + ' 需 LibreOffice 转换；请安装 LibreOffice 或另存为 .xlsx/.pptx 后上传')
    target = 'xlsx' if extension == '.xls' else 'pptx'
    with tempfile.TemporaryDirectory(prefix='skill-lab-convert-') as folder:
        root = Path(folder)
        profile = root / 'profile'
        profile.mkdir()
        (profile / 'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item></oor:items>''', encoding='utf-8')
        source = root / ('source' + extension)
        source.write_bytes(raw)
        try:
            process = subprocess.run([executable, '-env:UserInstallation=' + profile.as_uri(), '--headless',
                '--norestore', '--nodefault', '--nofirststartwizard', '--convert-to', target,
                '--outdir', folder, str(source)], capture_output=True, timeout=45,
                env={**os.environ, 'SAL_USE_VCLPLUGIN': 'svp'})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('旧版文档转换失败或超时，请另存为现代格式后上传') from exc
        output = root / ('source.' + target)
        if process.returncode or not output.is_file():
            raise RuntimeError('旧版文档无法转换，可能加密或损坏；请另存为现代格式后上传')
        converted = output.read_bytes()
        _zip_check(converted)
        text, warnings, units = (_xlsx if target == 'xlsx' else _pptx)(converted)
        return text, ['旧版文档经 LibreOffice 转换后提取，保留原始附件供核对。'] + warnings, units


def parse_document(name, mime, raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > 10 * 1024 * 1024:
        raise RuntimeError('文档不能为空且不能超过 10 MB')
    if raw.startswith(b'%TSD-Header-'):
        raise RuntimeError('文档受保护，不能解析；请提供正式解密后的文件')
    extension = Path(str(name)).suffix.lower()
    try:
        if extension in ('.xlsx', '.pptx'):
            _zip_check(raw)
            text, warnings, units = (_xlsx if extension == '.xlsx' else _pptx)(raw)
        elif extension == '.pdf':
            if not raw.startswith(b'%PDF-'):
                raise RuntimeError('PDF 文件头无效')
            text, warnings, units = _pdf(raw)
        elif extension == '.json':
            text, warnings, units = _json(raw)
        elif extension in ('.xls', '.ppt'):
            if not raw.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
                raise RuntimeError('旧版 Office 文件头无效，请核对文件格式')
            text, warnings, units = _legacy(raw, extension)
        else:
            raise RuntimeError('不支持的文档类型')
        if len(text) > MAX_TEXT:
            raise RuntimeError('提取文本超过 120000 字，请拆分文件后上传')
        return dict(name=name, format=extension[1:], text=text, warnings=warnings, units=units)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError('文档解析失败，可能损坏、格式不符或结构超出限制') from exc


if __name__ == '__main__':
    try:
        payload = json.load(sys.stdin)
        output = parse_document(payload['name'], payload.get('mime', ''), base64.b64decode(payload['data'], validate=True))
        print(json.dumps(output, ensure_ascii=False))
    except Exception as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else '文档解析请求无效', file=sys.stderr)
        sys.exit(1)
