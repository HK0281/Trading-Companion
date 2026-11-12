"""Replay local source bytes through production decoders and parsers, without network I/O."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

MAX_MANIFEST_BYTES = 1_048_576
MAX_SAMPLES = 256
MAX_DOCUMENT_BYTES = 512_000


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _read_bytes(path, maximum):
    with path.open('rb') as stream:
        body = stream.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError('bounded local read exceeded')
    return body


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError('duplicate metadata key')
        result[key] = value
    return result


def _retrieved_at(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('timezone required')
        return stamp.isoformat()
    except (ValueError, TypeError, AttributeError):
        raise argparse.ArgumentTypeError('必须提供带时区的 ISO 8601 时间。') from None


def _sample_path(root, value, kind):
    if type(value) is not str or not 1 <= len(value) <= 240 or not value.isprintable():
        raise ValueError('invalid sample path')
    windows = PureWindowsPath(value)
    relative = PurePosixPath(value.replace('\\', '/'))
    if (windows.drive or windows.root or relative.is_absolute() or '..' in relative.parts
            or ':' in value or not relative.parts):
        raise ValueError('sample path escapes root')
    suffixes = ('.html', '.htm') if kind == 'news' else ('.json',)
    if relative.suffix.lower() not in suffixes:
        raise ValueError('invalid sample extension')
    resolved = root.joinpath(*relative.parts).resolve()
    if not resolved.is_relative_to(root) or resolved.suffix.lower() not in suffixes:
        raise ValueError('sample path escapes root')
    return resolved


def _media(row, kind):
    declared = row.get('content_type')
    if declared is not None:
        if type(declared) is not str or not declared.strip():
            raise ValueError('invalid content type')
        return declared, 'metadata'
    headers = row.get('headers', row.get('response_headers', {}))
    if type(headers) is not dict:
        raise ValueError('invalid saved headers')
    values = [value for key, value in headers.items() if type(key) is str and key.lower() == 'content-type']
    if values:
        if any(type(value) is not str or not value.strip() for value in values) or len(set(values)) != 1:
            raise ValueError('invalid saved headers')
        return values[0], 'metadata'
    return ('text/html' if kind == 'news' else 'application/json'), 'default'


def replay_sample(root, row, index, *, retrieved_at=None):
    from trading_companion.company_announcement_body import parse_eastmoney_announcement
    from trading_companion.company_news_body import parse_sina_article
    from trading_companion.document_collection import decode_document_payload
    from trading_companion.research_documents import DocumentCandidate, DocumentError

    result = dict(sample=index, status='unavailable', code='replay_invalid_metadata',
                  characters=0, excerpts=0, historical_acceptance=False,
                  time_basis='cli_override' if retrieved_at else 'file_mtime')
    if type(row) is not dict or row.get('kind') not in ('news', 'announcements'):
        return result
    kind = row['kind']
    try:
        path = _sample_path(root, row.get('file'), kind)
    except (ValueError, OSError, RuntimeError):
        result['code'] = 'replay_path_blocked'
        return result
    filename = row['file'].replace('\\', '/')
    if re.fullmatch(r'[A-Za-z0-9_./-]{1,240}', filename):
        result['file'] = filename
    try:
        body = _read_bytes(path, MAX_DOCUMENT_BYTES)
        observed = retrieved_at or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except FileNotFoundError:
        result['code'] = 'replay_file_missing'
        return result
    except ValueError:
        result['code'] = 'document_too_large'
        return result
    except (OSError, OverflowError):
        result['code'] = 'replay_read_failed'
        return result
    try:
        if row.get('publication_precision') not in ('date', 'datetime'):
            return result
        media, result['media_basis'] = _media(row, kind)
        candidate = DocumentCandidate(row.get('evidence_id'), row.get('instrument_id'), row.get('security_name'),
            kind, row.get('source', 'sina_company_news' if kind == 'news' else 'eastmoney_announcements'),
            row.get('title'), row.get('url'), row.get('published_date'), row.get('published_at'))
        payload = decode_document_payload(body, kind, media)
        parse = parse_sina_article if kind == 'news' else parse_eastmoney_announcement
        document = parse(payload, candidate, retrieved_at=observed)
        excerpts = document.excerpts(candidate.title)
    except DocumentError as error:
        result['code'] = error.code
    except (ValueError, UnicodeError, TypeError, KeyError, RecursionError):
        result['code'] = 'document_invalid_response'
    except Exception:
        result['code'] = 'replay_parser_failed'
    else:
        result.update(status='available', code='document_read' if document.complete else document.incomplete_reasons[0],
                      characters=len(document.text), excerpts=len(excerpts), **document.coverage_metadata())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线回放本地正文样本；不联网、不读 .env，不输出正文。')
    parser.add_argument('--samples', required=True, type=Path,
        help='包含 candidates.json 的目录，或该清单文件；不存在时输出 skipped 并成功退出。')
    parser.add_argument('--retrieved-at', type=_retrieved_at,
        help='显式覆盖所有正文的观察时刻（带时区）。默认使用各正文文件 mtime，'
             '仅作本地诊断观察时刻；不采用候选标题的 retrieved_at，不声称历史验收。')
    args = parser.parse_args(argv)
    supplied = args.samples.absolute()
    if not supplied.exists():
        _emit(dict(status='skipped', code='replay_samples_missing', historical_acceptance=False))
        return 0
    root = (supplied if supplied.is_dir() else supplied.parent).resolve()
    manifest = (root / 'candidates.json' if supplied.is_dir() else supplied).resolve()
    if not manifest.is_relative_to(root):
        _emit(dict(status='unavailable', code='replay_path_blocked', historical_acceptance=False))
        return 1
    if manifest.name != 'candidates.json':
        _emit(dict(status='unavailable', code='replay_invalid_manifest', historical_acceptance=False))
        return 1
    if not manifest.exists():
        _emit(dict(status='skipped', code='replay_manifest_missing', historical_acceptance=False))
        return 0
    try:
        rows = json.loads(_read_bytes(manifest, MAX_MANIFEST_BYTES).decode('utf-8-sig'), object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
        if type(rows) is not list or len(rows) > MAX_SAMPLES:
            raise ValueError('invalid sample manifest')
    except (OSError, ValueError, UnicodeError, RecursionError):
        _emit(dict(status='unavailable', code='replay_invalid_manifest', historical_acceptance=False))
        return 1
    available, complete, partial = 0, 0, 0
    for index, row in enumerate(rows, 1):
        result = replay_sample(root, row, index, retrieved_at=args.retrieved_at)
        available += result['status'] == 'available'
        complete += result.get('complete') is True
        partial += result.get('complete') is False
        _emit(result)
    _emit(dict(status='completed', code='replay_completed', samples=len(rows), available=available,
               complete=complete, partial=partial, unavailable=len(rows) - available, historical_acceptance=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
