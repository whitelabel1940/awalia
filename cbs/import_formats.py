"""Parse export syntax only. No network, file extraction or semantic filtering."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
import re
from urllib.parse import urlparse
import zipfile

from cbs import clock

MAX_BYTES = 64 * 1024 * 1024
MAX_MESSAGES = 200_000
MAX_CONVERSATIONS = 10_000


def timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None
            if abs(value) > 100_000_000_000:
                value /= 1000
            return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat()
        value = str(value).strip().removeprefix('> ').strip()
        try:
            return clock.parse(value).isoformat()
        except ValueError:
            for fmt in ('%Y/%m/%d %H:%M:%S', '%m/%d/%Y %H:%M:%S', '%Y/%m/%d', '%m/%d/%Y'):
                try:
                    return clock.parse(datetime.strptime(value, fmt)).isoformat()
                except ValueError:
                    pass
    except (ValueError, OverflowError, OSError):
        pass
    return None


def clean_text(text):
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '（画像）', text, flags=re.S)
    return re.sub(r'data:[^\s)]+', '（添付）', text).strip()


def body(value):
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return '\n'.join(filter(None, (body(v) for v in value)))
    if not isinstance(value, dict):
        return ''
    kind = str(value.get('type') or value.get('content_type') or '')
    if any(k in value for k in ('image_url', 'asset_pointer')) or kind in ('image', 'image_asset_pointer'):
        return '（画像）'
    if kind in ('file', 'attachment', 'document', 'audio', 'video'):
        return '（添付）'
    if isinstance(value.get('text'), str):
        return clean_text(value['text'])
    return body(value.get('parts', []))


@dataclass
class Message:
    role: str
    text: str
    at: str | None
    time_source: str = 'message'


@dataclass
class Conversation:
    format: str
    source_id: str
    title: str
    messages: list[Message]
    warnings: list[str]
    error: str | None = None

    @property
    def content_hash(self):
        # A deterministic digest, not an LLM summary. Keep dates: identical
        # greetings on different days are separate conversations.
        value = [(m.role, ' '.join(m.text.split()), m.at) for m in self.messages]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    @property
    def key(self):
        return hashlib.sha256((self.format + ':' + self.source_id + ':' + self.content_hash).encode()).hexdigest()

    def listing(self):
        dates = [m.at for m in self.messages if m.at]
        return {'key': self.key, 'format': self.format, 'source_id': self.source_id, 'title': self.title,
                'first_at': min(dates) if dates else None, 'last_at': max(dates) if dates else None,
                'message_count': len(self.messages), 'content_hash': self.content_hash,
                'inferred_times': sum(m.time_source != 'message' for m in self.messages),
                'warnings': self.warnings, 'error': self.error}

    def dump(self):
        return asdict(self)

    @classmethod
    def load(cls, value):
        return cls(**{**value, 'messages': [Message(**m) for m in value['messages']]})


ROLES = {'user': 'user', 'human': 'user', 'prompt': 'user', 'assistant': 'assistant', 'response': 'assistant',
         # AI Chat Exporter family (chatgptexporter.com / ai-chat-exporter.com / .net) names the speaker.
         'gemini': 'assistant', 'chatgpt': 'assistant', 'claude': 'assistant'}
SPEAKERS = '|'.join(['Prompt', 'User', 'Response', 'Assistant', 'Gemini', 'ChatGPT', 'Claude'])


def finish(fmt, data, raw, *, metadata=None):
    meta = metadata or data
    dates = meta.get('dates') or {}
    start = timestamp(data.get('create_time') or data.get('created_at') or dates.get('created'))
    end = timestamp(data.get('update_time') or data.get('updated_at') or dates.get('updated'))
    messages, warnings = [], []
    for role, text, at, attachments in raw:
        role = ROLES.get(str(role).lower())
        if not role:
            continue
        text = body(text)
        if attachments:
            text += '\n' + '\n'.join('（画像）' if a.get('file_type', '').startswith('image') else '（添付）' for a in attachments if isinstance(a, dict))
        if text.strip():
            messages.append(Message(role, text.strip(), timestamp(at)))
    if len(messages) > MAX_MESSAGES:
        raise ValueError('発言が多すぎます。ファイルを分けてください。')
    anchors = [m.at for m in messages if m.at]
    fallback = start or end or (anchors[0] if anchors else None)
    # Only missing timestamps are filled. Real dates never get shifted to make
    # ordering convenient. Equal inferred dates retain original file order.
    for m in messages:
        if not m.at and fallback:
            m.at = fallback
            m.time_source = 'conversation_created' if start else 'conversation_updated' if end else 'message_anchor'
        elif m.at:
            fallback = m.at
    if any(m.time_source != 'message' for m in messages):
        warnings.append('欠けた時刻を会話の作成・更新日時または直前の発言日時から補いました。')
    if messages and all(m.at for m in messages):
        if any(clock.parse(a.at) > clock.parse(b.at) for a, b in zip(messages, messages[1:])):
            warnings.append('発言を元の日時順に並べました（同時刻は元の順番）。')
        messages.sort(key=lambda m: clock.parse(m.at))
    identifier = str(data.get('id') or data.get('uuid') or data.get('conversation_id') or '')
    link = meta.get('link')
    if not identifier and isinstance(link, str):
        identifier = urlparse(link).path.rstrip('/').rsplit('/', 1)[-1]
    title = str(meta.get('title') or data.get('name') or data.get('title') or '題名のない会話')[:500]
    identifier = identifier or hashlib.sha256((title + str(start or end)).encode()).hexdigest()
    error = '日付の根拠がありません。日時のある書き出しを選んでください。' if any(not m.at for m in messages) else None
    if not messages:
        error = '取り込めるユーザーと相手の発言がありません。'
    if any(len(m.text)>20000 for m in messages):
        error = '2万文字を超える発言があります。発言を分けた書き出しを選んでください。'
    return Conversation(fmt, identifier, title, messages, warnings, error)


def parse_json(data):
    entries = data if isinstance(data, list) else data.get('conversations', [data]) if isinstance(data, dict) else []
    if len(entries) > MAX_CONVERSATIONS:
        raise ValueError('会話が多すぎます。ファイルを分けてください。')
    result = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError('会話の形式を確認してください。')
        raw = []
        if 'mapping' in item:
            mapping = item['mapping']
            current, seen, branch = item.get('current_node'), set(), []
            # Without a selected branch, do not blend mutually exclusive edits.
            if not isinstance(mapping, dict) or current not in mapping:
                result.append(Conversation('chatgpt', str(item.get('id', '')), str(item.get('title', '')), [], [], '会話の現在の枝がありません。'))
                continue
            while current is not None:
                if current in seen or current not in mapping:
                    raise ValueError('会話の枝が壊れています。')
                seen.add(current)
                node = mapping[current]
                branch.append(node.get('message'))
                current = node.get('parent')
            for m in reversed(branch):
                if not isinstance(m, dict) or (m.get('metadata') or {}).get('is_visually_hidden_from_conversation'):
                    continue
                if m.get('channel') not in (None, 'final') or m.get('recipient') not in (None, 'all'):
                    continue
                raw.append(((m.get('author') or {}).get('role'), m.get('content'), m.get('create_time'), []))
            result.append(finish('chatgpt', item, raw))
        elif 'chat_messages' in item:
            for m in item['chat_messages']:
                raw.append((m.get('sender'), m.get('text') or m.get('content'), m.get('created_at'), m.get('attachments', [])))
            result.append(finish('claude', item, raw))
        elif 'messages' in item:
            for m in item['messages']:
                if m.get('checked') is False:  # Left unselected in the exporter.
                    continue
                parts = m.get('contents')
                if isinstance(parts, list):
                    # Gemini Exporter splits a reply into thinking/text parts; `text` mixes both.
                    content = [p.get('content', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text']
                else:
                    content = m.get('say', m.get('content', m.get('text', '')))
                raw.append((m.get('role'), content, m.get('time') or m.get('timestamp') or m.get('created_at'), m.get('attachments', [])))
            result.append(finish('exporter', item, raw, metadata=item.get('metadata')))
        else:
            raise ValueError('対応する書き出し形式ではありません。')
    return result


def parse_markdown(text):
    meta, raw, role, lines, at = {}, [], None, [], None
    fence, thinking, unknown, pending = None, False, None, None
    def flush():
        if role:
            raw.append((role, '\n'.join(lines).strip(), at, []))
    for line in text.splitlines():
        if re.match(r'^\s*(```|~~~)', line):
            marker = line.strip()[:3]
            fence = None if fence == marker else marker if fence is None else fence
        heading = re.fullmatch(rf'##\s+({SPEAKERS}):?\s*', line, re.I) if fence is None else None
        # An exporter speaker heading we do not know ("## Name:" then a timestamp) must not
        # be folded silently into the previous speaker's text.
        if pending is not None and line.strip():
            if timestamp(line) and not unknown:
                unknown = pending
            pending = None
        if fence is None and not heading:
            other = re.fullmatch(r'##\s+([^\s#:][^#:]{0,40}):\s*', line)
            if other:
                pending = other[1]
        if heading:
            flush()
            role, lines, at, thinking = heading[1].lower(), [], None, False
        elif role:
            # Gemini Exporter quotes the model's "Thinking steps" before the answer; they are not the reply.
            if not lines and fence is None and re.match(r'^>\s*\*\*(Thinking|Thought)\b', line):
                thinking = True
                continue
            if thinking:
                if line.startswith('>') or not line.strip():
                    continue
                thinking = False
            if not lines and timestamp(line):
                at = line.strip().removeprefix('> ')
            elif re.match(r'^Powered by ', line) and fence is None:
                flush()
                role = None
            elif lines or line.strip():
                lines.append(line)
        else:
            if line.startswith('# '):
                meta['title'] = line[2:].strip()
            match = re.match(r'^(?:\*\*)?(Created|Updated|Link):(?:\*\*)?\s*(.+)', line, re.I)
            if match:
                key, value = match[1].lower(), match[2]
                if key == 'link':
                    link = re.search(r'\]\(([^)]+)\)', value)
                    meta['link'] = link[1] if link else value
                else:
                    meta.setdefault('dates', {})[key] = value
    flush()
    conversation = finish('exporter', {'metadata': meta}, raw, metadata=meta)
    if unknown:
        conversation.error = f'発言の見出し「{unknown}」を読み取れませんでした。対応している書き出しを選んでください。'
    return [conversation]


def _parse_export(filename, payload):
    if len(payload) > MAX_BYTES:
        raise ValueError('書き出しは64 MiB以下に分けてください。')
    if filename.lower().endswith('.zip'):
        result = []
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            files = [i for i in archive.infolist() if re.search(r'(?:^|/)conversations(?:-\d+)?\.json$', i.filename)]
            if not files or sum(i.file_size for i in files) > MAX_BYTES:
                raise ValueError('zip内の会話JSONがないか、展開後の上限64 MiBを超えています。')
            for item in sorted(files, key=lambda i: i.filename):
                result.extend(parse_json(json.loads(archive.read(item).decode('utf-8-sig'))))
    else:
        text = payload.decode('utf-8-sig')
        result = parse_markdown(text) if filename.lower().endswith(('.md', '.markdown')) else parse_json(json.loads(text))
    if len(result) > MAX_CONVERSATIONS or sum(len(c.messages) for c in result) > MAX_MESSAGES:
        raise ValueError('会話または発言が多すぎます。ファイルを分けてください。')
    return result


def parse_export(filename, payload):
    try:
        return _parse_export(filename,payload)
    except (AttributeError,TypeError,KeyError,RecursionError) as error:
        raise ValueError('書き出しの構造が壊れています。元のファイルを選んでください。') from None
