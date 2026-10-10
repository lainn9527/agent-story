"""Edit a completed turn using its pre-turn state and optional draft, never its future."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import shutil
import uuid

from story_core import story_io as io, compaction
from story_core.card_mode import branches, ensemble
from story_core.card_mode.card import load_card


def revision_prompt(draft, instruction):
    return ('\n\n【本輪編輯要求：不是故事中的對話或事件】\n'
            '重新演出玩家本輪輸入，只輸出完整的新回覆，不解釋修改。編輯要求不得成為角色知道的事。\n'
            + ('以下舊回覆只是待修改草稿，不是已發生歷史。保留未被要求修改且與新玩家輸入相容的段落、語氣與節奏；'
               '只調整要求的部分及必要的連帶矛盾，不任意重寫全部。\n【待修改草稿】\n'+draft+'\n【草稿結束】\n' if draft is not None else
               '完全重新生成，不沿用被替換回覆的內容。\n')
            + '【修改指示】\n'+(instruction or '依本輪玩家輸入重新演出。'))


def validate(body):
    if not isinstance(body, dict): raise branches.BranchError('請提供編輯內容')
    if type(body.get('message_index')) is not int: raise branches.BranchError('請選擇要編輯的回合')
    if body.get('mode') not in ('regenerate', 'revise'): raise branches.BranchError('無效的生成方式')
    if body.get('destination') not in ('fork', 'overwrite'): raise branches.BranchError('無效的儲存方式')
    ensemble.validate_turn(body.get('message'), body.get('response_length'), body.get('recall_mode'), body.get('narrative_style'), body.get('plot_mode'), body.get('scene_mode'))
    instruction = body.get('instruction', '')
    if not isinstance(instruction, str) or len(instruction) > 20000: raise branches.BranchError('修改指示最多 20000 字')
    if not isinstance(body.get('expected_reply'), str): raise branches.BranchError('缺少原回覆，請重新載入後再編輯')


def edit_turn(slug, source, body, *, emit_chunk=None):
    from story_core.card_mode import pipeline, ensemble_recorder
    validate(body); load_card(slug)
    cast = ensemble.enabled(slug)
    guard = ensemble.lock(slug, branch_id=source) if cast else branches.lock(slug, source)
    with guard:
        doc = ensemble.load(slug, source) if cast else None
        messages = doc['messages'] if cast else io._load_branch_messages(slug, source)
        pos = next((i for i,m in enumerate(messages) if m.get('index') == body['message_index'] and m.get('role') == 'gm'), -1)
        if pos < 2 or messages[pos-1].get('role') != 'user': raise branches.BranchError('開場不能編輯重生')
        old = messages[pos]
        if old.get('content') != body['expected_reply']: raise branches.BranchError('原回覆已改變，請重新載入後再編輯')
        if cast and old['index'] <= doc['baseline_index']: raise branches.BranchError('不能編輯副本建立前的回合')
        prefix = deepcopy(messages[:pos-1])
        if cast:
            if not isinstance(old.get('ensemble_pre_snapshot'), dict): raise branches.BranchError('缺少回合前快照，無法安全編輯')
        elif not (isinstance(old.get('pre_state_snapshot'), dict)
                  and isinstance(old.get('pre_npcs_snapshot'), list)
                  and isinstance(old.get('pre_clock_snapshot'), dict)
                  and bool(old['pre_clock_snapshot'])):
            raise branches.BranchError('缺少回合前快照，無法安全編輯')
        if cast:
            branches.ensure_settled(prefix, for_fork=True)
        else:
            branches.ensure_snapshot_prefix(prefix, old, before_turn=True)
        draft = old['content'] if body['mode'] == 'revise' else None
        directive = revision_prompt(draft, body.get('instruction',''))
        forked = body['destination'] == 'fork'
        target = 'card_'+uuid.uuid4().hex[:12] if forked else source
        dest = Path(io.STORIES_DIR) / slug / 'branches' / target
        backups = {}
        if forked:
            with branches.lock(slug, '_tree'):
                tree = branches.tree(slug)
                stamp = datetime.now(timezone.utc).isoformat()
                dest.mkdir(parents=True, exist_ok=False)
                tree['branches'][target] = {'id':target, 'name':f'編輯 #{old["index"]}', 'parent_branch_id':source,
                    'branch_point_index':prefix[-1]['index'], 'card_full_snapshot':True, 'created_at':stamp,'updated_at':stamp}
                io._save_json(io._story_tree_path(slug), tree)
        try:
            # Keep the new branch locked until generation commits, too.
            target_guard = (ensemble.lock(slug, branch_id=target) if cast else branches.lock(slug, target)) if forked else nullcontext()
            with target_guard:
                if forked:
                    from story_core.card_mode import candidates, instructions
                    candidates.copy_archive(slug, source, target, prefix)
                    instructions.inherit(slug,source,target)
                if cast:
                    candidate = deepcopy(doc)
                    candidate['messages'] = prefix
                    candidate['state'] = deepcopy(old['ensemble_pre_snapshot'])
                    candidate['ensemble_pending_controls'] = deepcopy(old.get('ensemble_controls_before', []))
                    if pos == len(messages)-1:
                        controls = deepcopy(doc.get('ensemble_pending_controls', []))
                        ensemble.apply_controls(candidate['state'], controls)
                        candidate['ensemble_pending_controls'] += controls
                    # The inherited recap must not expose the edited/future turns.
                    if candidate.get('source_recap', {}).get('compacted_through_index', -1) >= len(prefix):
                        candidate['source_recap'] = {}
                    candidate, result = ensemble.turn(slug, candidate, body['message'], body.get('response_length'),
                        recall_mode=body.get('recall_mode'), emit_chunk=emit_chunk, branch_id=target, revision=directive, narrative_style=body.get('narrative_style'), plot_mode=body.get('plot_mode'), scene_mode=body.get('scene_mode'))
                    ensemble.save(slug, candidate, target)
                else:
                    names = ['messages.json','character_state.json','npcs.json','card_clock.json','conversation_recap.json','card_generation.json']
                    backups = {name:(dest/name).read_bytes() if (dest/name).exists() else None for name in names}
                    branches.invalidate_generation(slug, target)
                    for field,name in [('pre_state_snapshot','character_state.json'),('pre_npcs_snapshot','npcs.json'),('pre_clock_snapshot','card_clock.json')]:
                        io._save_json(str(dest/name), deepcopy(old[field]))
                    io._save_json(str(dest/'messages.json'), prefix)
                    if forked:
                        recap = compaction.load_recap(slug, source)
                        io._save_json(str(dest/'conversation_recap.json'), deepcopy(recap))
                    compaction.invalidate_recap(slug, target, retained_message_count=len(prefix))
                    result = pipeline.run_card_turn(slug, target, body['message'], emit_chunk=emit_chunk,
                        response_length=body.get('response_length'), recall_mode=body.get('recall_mode'), revision=directive, narrative_style=body.get('narrative_style'), plot_mode=body.get('plot_mode'), scene_mode=body.get('scene_mode'))
                    if not result.get('ok'): raise ValueError(result.get('error','生成失敗'))
        except Exception:
            if forked:
                with branches.lock(slug, '_tree'):
                    tree = branches.tree(slug); tree['branches'].pop(target, None)
                    if tree.get('active_branch_id') == target: tree['active_branch_id'] = source
                    io._save_json(io._story_tree_path(slug), tree)
                    shutil.rmtree(dest, ignore_errors=True)
            elif not cast:
                for name, content in backups.items():
                    p = dest/name
                    if content is None: p.unlink(missing_ok=True)
                    else:
                        tmp = p.with_suffix('.restore'); tmp.write_bytes(content); tmp.replace(p)
            raise
        if forked:
            with branches.lock(slug, '_tree'):
                tree = branches.tree(slug); tree['active_branch_id'] = target
                io._save_json(io._story_tree_path(slug), tree)
        if cast: ensemble_recorder.start(slug, branch_id=target)
        return {**result, 'branch_id':target}
