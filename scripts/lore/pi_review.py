#!/usr/bin/env python3
"""Read-only Pi lore audit. One fresh session per entry; output is a trial report."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from story_core import story_io as io
from story_core.card_mode import lore
from story_core.card_mode.pi_lore_review import run_trial, snapshot, still_current
from story_core.branch_tree import get_full_timeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('slug')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--entry', action='append', required=True)
    parser.add_argument('--pi-cli', required=True, help='Installed Pi executable or CLI .js path')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    if len(args.entry) > 4 or len(set(args.entry)) != len(args.entry):
        parser.error('每批最多四條且不可重複')
    if not 1 <= args.timeout <= 600:
        parser.error('timeout 必須介於 1 與 600 秒')
    config = io._load_json('llm_config.json', {}).get('vertex', {})
    if not all(config.get(k) for k in ('project', 'model')):
        parser.error('需要既有 Vertex project/model 設定及 ADC 驗證')
    messages = get_full_timeline(args.slug, args.branch)
    entries = {e['id']: e for e in lore.available(args.slug, args.branch, messages)}
    # Validate the entire requested batch before making any model call.
    try:
        frozen = [snapshot(entries[eid], messages) for eid in args.entry]
    except KeyError as error:
        parser.error(f'條目不屬於目前可用 lore：{error.args[0]}')
    except ValueError as error:
        parser.error(str(error))
    reports = []
    for item in frozen:
        report = run_trial(item, cli=args.pi_cli, vertex=config, timeout=args.timeout)
        current = get_full_timeline(args.slug, args.branch)
        if not still_current(item, lore.available(args.slug, args.branch, current), current):
            report.update(status='stale', error='原文或 lore 在測試期間已更換；本報告不可沿用')
            Path(report['run_dir'], 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if all(r['status'] == 'reviewed' for r in reports) else 1


if __name__ == '__main__':
    sys.exit(main())
