import copy
import tomllib

from cbs import backup, restore
from cbs.config import ROOT, overlay
from cbs.web.settings_api import remember_partner, toml_text


def test_restore_keeps_only_the_old_pcs_own_choices(tmp_path):
    base = tomllib.loads((ROOT / 'config.toml').read_text(encoding='utf-8'))
    # The old PC ran an older version whose default model candidates differed.
    exported = copy.deepcopy(base)
    exported['simple']['actor_candidates'][0]['label'] = '古い既定の名前'
    saved = {'character': {'id': 'partner_b'}, 'tts': {'enabled': True},
             'llm': {'mode': exported['llm']['mode']}}
    content = tmp_path / 'content'
    (content / 'data').mkdir(parents=True)
    (content / 'config.toml').write_bytes(backup.toml_bytes(exported))
    (content / 'data/settings.toml').write_bytes(backup.toml_bytes(saved))
    settings = restore.restored_settings(content, base, {}, ['partner_a', 'partner_b'])
    assert settings['character'] == {'id': 'partner_b'}
    assert settings['tts'] == {'enabled': True}
    # Neither the old defaults nor values equal to the defaults are pinned into settings.toml.
    assert 'simple' not in settings and 'llm' not in settings


def old_pc(tmp_path, saved):
    base = tomllib.loads((ROOT / 'config.toml').read_text(encoding='utf-8'))
    content = tmp_path / 'content'
    (content / 'data').mkdir(parents=True)
    (content / 'config.toml').write_bytes(backup.toml_bytes(base))
    (content / 'data/settings.toml').write_bytes(backup.toml_bytes(saved))
    return content, base


def test_restore_never_carries_the_old_pcs_backup_folder(tmp_path):
    # The folder belongs to the old PC; the same PC would end up with two installs sharing it.
    saved = {'character': {'id': 'partner_a'}, 'backup': {'folder': r'D:\Sync\Awalia', 'frequency': 'daily', 'export_keep': 6}}
    content, base = old_pc(tmp_path, saved)
    settings = restore.restored_settings(content, base, {}, ['partner_a'])
    assert settings['backup'] == {'export_keep': 6}
    assert restore.scheduled_folder(restore.exported_settings(content)[1]) == r'D:\Sync\Awalia'
    assert restore.scheduled_folder(overlay(base, settings)) == ''
    # This PC's own folder choice stays.
    mine = {'backup': {'folder': r'E:\OneDrive\Awalia', 'frequency': 'weekly'}}
    settings = restore.restored_settings(content, base, mine, ['partner_a'])
    assert restore.scheduled_folder(overlay(base, settings)) == r'E:\OneDrive\Awalia'
    assert restore.scheduled_folder({'backup': {'folder': r'D:\x', 'frequency': 'off'}}) == ''


def test_settings_writer_keeps_arrays_of_tables_valid(tmp_path):
    # A restore writes the exported config into settings.toml, including arrays of tables.
    # The next settings save used to turn them into JSON-style inline tables, so the file
    # failed to parse and every later save (and the next start) stopped.
    restored = tomllib.loads((ROOT / 'config.toml').read_text(encoding='utf-8'))
    assert restored['simple']['actor_candidates']
    path = tmp_path / 'settings.toml'
    path.write_text(toml_text(restored), encoding='utf-8')
    saved = overlay(tomllib.loads(path.read_text(encoding='utf-8')), {'llm': {'mode': 'simple'}})
    path.write_text(toml_text(saved), encoding='utf-8')
    again = tomllib.loads(path.read_text(encoding='utf-8'))
    assert again['simple']['actor_candidates'] == restored['simple']['actor_candidates']
    assert again['llm']['mode'] == 'simple'
    remember_partner(tmp_path, 'partner_a')
    assert tomllib.loads(path.read_text(encoding='utf-8'))['character']['id'] == 'partner_a'
