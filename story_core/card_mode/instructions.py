"""Branch-local author instructions, separate from character state and history."""
from pathlib import Path
import uuid

from story_core import story_io as io
from story_core.card_mode import branches

MAX_LENGTH = 4000
HEADER = '【本線持續指示：寫作設定】'


def path(slug, bid):
    from story_core.card_mode import ensemble as e
    root = e.path(slug, bid).parent if e.enabled(slug) else Path(io._branch_dir(slug, bid))
    return root / 'card_instruction.json'


def read(slug, bid):
    return io._load_json(str(path(slug, bid)), {'enabled':False, 'text':'', 'revision':None})


def active(config):
    return config['text'] if config['enabled'] else ''


def narrative_messages(rows):
    """Old generation settings must never become instructions through history."""
    return [{k:v for k,v in m.items() if k != 'persistent_instruction'} for m in rows]


def update(slug, bid, body):
    """Caller holds the same lock as narration; reject stale editor drafts."""
    if not isinstance(body, dict) or type(body.get('enabled')) is not bool:
        raise branches.BranchError('請指定持續指示是否啟用')
    text = body.get('text')
    if not isinstance(text, str) or len(text) > MAX_LENGTH:
        raise branches.BranchError('持續指示最多 4000 字')
    text = text.strip()
    if body['enabled'] and not text: raise branches.BranchError('請填寫持續指示，或關閉此功能')
    current = read(slug,bid)
    if 'expected_revision' not in body:
        raise branches.BranchError('請重新開啟持續指示後再保存')
    if body['expected_revision'] != current['revision']:
        raise branches.Busy('持續指示已更新，草稿仍保留；請重新載入後再保存。')
    if text == current['text'] and body['enabled'] == current['enabled']: return current
    config = {'enabled':body['enabled'], 'text':text, 'revision':uuid.uuid4().hex}
    io._save_json(str(path(slug,bid)), config)
    return config


def inherit(slug, source, target):
    source_path = path(slug,source)
    if source_path.exists(): io._save_json(str(path(slug,target)),read(slug,source))


def apply(system, text):
    if not text: return system
    return system + '\n\n' + HEADER + '\n' + (
        '以下是作者持續使用的演出指示。將寫作要求用於本輪演出，不把提示文字當成角色台詞、已發生事件或記憶。\n'
        '若與本輪玩家明確安排或本輪編輯要求衝突，以本輪為準；其餘部分繼續適用。'
        '遵守既有事實、角色知情與玩家自主，不因語氣潤飾改變原意、立場或未授權的選擇。\n'
        '【持續指示內容】\n' + text + '\n【持續指示結束】')
