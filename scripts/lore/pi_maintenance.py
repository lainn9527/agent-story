#!/usr/bin/env python3
"""Run a bounded evidence-backed maintenance task; scheduling is deliberately separate."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from story_core import story_io as io
from story_core.card_mode import pi_maintenance as tasks


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    run = sub.add_parser('run')
    run.add_argument('slug'); run.add_argument('--branch', default='main')
    run.add_argument('--kind', action='append', choices=['memory', 'lore', 'summary', 'inventory'])
    run.add_argument('--recent', type=int, default=20)
    run.add_argument('--focus', action='append', default=[])
    run.add_argument('--pi-cli', required=True)
    run.add_argument('--timeout', type=int, default=300)
    run.add_argument('--apply', action='store_true', help='Automatically apply only after evidence/revision gates pass')
    schedule = sub.add_parser('schedule', help='Enable background maintenance after completed pairs')
    schedule.add_argument('slug'); schedule.add_argument('--branch', default='main')
    schedule.add_argument('--every', type=int, default=50)
    schedule.add_argument('--pi-cli'); schedule.add_argument('--disable', action='store_true')
    apply = sub.add_parser('apply'); apply.add_argument('run_dir')
    undo = sub.add_parser('undo'); undo.add_argument('backup')
    recover = sub.add_parser('recover'); recover.add_argument('backup')
    usage = sub.add_parser('usage', help='Read usage from saved events without a model call')
    usage.add_argument('run_dir')
    args = p.parse_args()
    try:
        if args.action == 'schedule':
            from story_core.card_mode import pi_schedule
            if args.disable:
                config = pi_schedule._read(pi_schedule.config_path(args.slug))
                config['enabled'] = False
                pi_schedule._save(pi_schedule.config_path(args.slug), config)
                print('disabled')
            else:
                if not args.pi_cli:
                    p.error('啟用排程需要 --pi-cli')
                pi_schedule.configure(args.slug, args.branch, cli=args.pi_cli, every=args.every)
                print(json.dumps({'status':'enabled','every_pairs':args.every},ensure_ascii=False))
        elif args.action == 'run':
            if not 1 <= args.timeout <= 600:
                p.error('timeout 必須介於 1–600 秒')
            vertex = io._load_json('llm_config.json', {}).get('vertex', {})
            if not all(vertex.get(k) for k in ('model', 'project')):
                p.error('需要既有 Vertex 設定及 ADC')
            frozen = tasks.freeze(args.slug, args.branch, kinds=args.kind or ('memory', 'lore'), recent=args.recent, focus=args.focus)
            result = tasks.run(frozen, cli=args.pi_cli, vertex=vertex, timeout=args.timeout)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if result['status'] != 'verified':
                return 1
            if args.apply:
                print(json.dumps(tasks.apply(result['run_dir']), ensure_ascii=False))
        elif args.action == 'usage':
            root = Path(args.run_dir)
            frozen = json.loads((root/'snapshot.json').read_text())
            report = json.loads((root/'report.json').read_text())
            print(json.dumps(tasks.pi_usage.collect(root, frozen, report, report.get('elapsed_ms')), ensure_ascii=False, indent=2))
        elif args.action == 'apply':
            print(json.dumps(tasks.apply(args.run_dir), ensure_ascii=False))
        elif args.action == 'undo':
            print(json.dumps(tasks.undo(args.backup), ensure_ascii=False))
        else:
            print(json.dumps(tasks.recover(args.backup), ensure_ascii=False))
    except (ValueError, OSError) as e:
        print(str(e), file=sys.stderr); return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
