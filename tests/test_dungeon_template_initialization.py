"""Card saves must not acquire unrelated RPG templates during startup."""
import json

from story_core import migrations, story_io, dungeon_system


def test_startup_skips_registered_unregistered_and_ensemble_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(dungeon_system,'_story_dir',lambda slug: str(tmp_path/'stories'/slug))
    stories=tmp_path/'stories';design=tmp_path/'design';registry=tmp_path/'stories.json'
    monkeypatch.setattr(story_io,'STORIES_DIR',str(stories))
    monkeypatch.setattr(story_io,'STORY_DESIGN_DIR',str(design))
    monkeypatch.setattr(story_io,'STORIES_REGISTRY_PATH',str(registry))
    registry.write_text(json.dumps({'stories':{'registered':{'mode':'card'},'rpg':{'mode':'story'}}}))
    for slug in ['registered','unregistered','ensemble','rpg','custom_rpg']:
        (stories/slug).mkdir(parents=True)
    (design/'unregistered').mkdir(parents=True)
    (design/'unregistered/card.json').write_text('{}')
    (stories/'ensemble/ensemble.json').write_text('{}')
    custom=b'{"dungeons": [{"id": "custom"}]}'
    (stories/'registered/dungeons_template.json').write_bytes(custom)
    (stories/'custom_rpg/dungeons_template.json').write_bytes(custom)
    migrations._init_dungeon_templates()
    assert not (stories/'unregistered/dungeons_template.json').exists()
    assert not (stories/'ensemble/dungeons_template.json').exists()
    assert (stories/'registered/dungeons_template.json').read_bytes()==custom
    assert (stories/'custom_rpg/dungeons_template.json').read_bytes()==custom
    generated=(stories/'rpg/dungeons_template.json').read_bytes()
    assert json.loads(generated)['dungeons']
    migrations._init_dungeon_templates()
    assert (stories/'rpg/dungeons_template.json').read_bytes()==generated
