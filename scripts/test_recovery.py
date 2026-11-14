#!/usr/bin/env python3
"""Offline recovery contracts, using synthetic games and injected filesystem faults."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import shutil
import subprocess
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py_modules'))
from optiscaler import constants, gameprofiles, installer, live, payload, restorepoint
from optiscaler.inifile import IniFile
from optiscaler.service import OptiScalerService


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*')
            if p.is_file() and restorepoint.RESTORE_DIR not in p.parts}


def filesystem_state(path):
    """Capture path type, contents and permissions for fail-closed checks."""
    path = Path(path)
    result = {".": ("dir", stat.S_IMODE(path.lstat().st_mode))}
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs:
            child = Path(base) / name
            result[child.relative_to(path).as_posix()] = (
                "dir", stat.S_IMODE(child.lstat().st_mode)
            )
        for name in files:
            child = Path(base) / name
            result[child.relative_to(path).as_posix()] = (
                "file", hashlib.sha256(child.read_bytes()).hexdigest(),
                stat.S_IMODE(child.lstat().st_mode),
            )
    return result


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / 'game'; self.game.mkdir()
        self.source = self.root / 'payload'; self.source.mkdir()
        manifest = json.loads((ROOT / 'defaults/runtime-manifest.json').read_text())
        for name in manifest['files']:
            p = self.source / name; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'synthetic support file')
        dll = installer.DLL_MARKER + b'\0' * installer.MIN_OPTISCALER_SIZE
        (self.source / 'OptiScaler.dll').write_bytes(dll)
        shutil.copy2(ROOT / 'defaults/OptiScaler.reference.ini', self.source / constants.INI_NAME)
        synthetic_pins = {name: {'bytes': (self.source / name).stat().st_size,
                                'sha256': hashlib.sha256((self.source / name).read_bytes()).hexdigest()}
                          for name in manifest['files']}
        inventory_pin = patch.object(payload, '_PINNED_FILES', synthetic_pins)
        inventory_pin.start(); self.addCleanup(inventory_pin.stop)
        self.pin = patch.object(constants, 'RUNTIME_DLL_SHA256', hashlib.sha256(dll).hexdigest())
        self.pin.start(); self.addCleanup(self.pin.stop)
        (self.game / 'dxgi.dll').write_bytes(b'original game dxgi')
        (self.game / 'user.txt').write_bytes(b'unrelated user file')
        self.original = tree(self.game)
        self.service = OptiScalerService(ROOT, self.root / 'settings', self.root / 'runtime',
                                         self.root / 'home', logging.getLogger('recovery'))
        self.service.ensure_payload = AsyncMock(return_value=str(self.source))
        self.service.refresh_wiki_if_stale = lambda: None
        self.service.refresh_page_if_stale = lambda page: None

    def install(self, **kwargs):
        return installer.install(self.game, self.source, **kwargs)

    def test_existing_ini_survives_both_install_modes_and_remove(self):
        original_ini = b'; original comments\r\n[FrameGen]\r\nEnabled=true\r\n'
        for preserve in (True, False):
            with self.subTest(preserve=preserve):
                (self.game / constants.INI_NAME).write_bytes(original_ini)
                self.install(preserve_ini=preserve)
                if preserve:
                    self.assertEqual((self.game / constants.INI_NAME).read_bytes(), original_ini)
                installer.uninstall(self.game)
                self.assertEqual((self.game / constants.INI_NAME).read_bytes(), original_ini)
                self.assertEqual((self.game / 'dxgi.dll').read_bytes(), self.original['dxgi.dll'])
                (self.game / constants.INI_NAME).unlink()

    def test_failed_first_install_restores_every_original(self):
        original = installer.atomic_copy
        failed = False
        def fail(source, destination, *args, **kwargs):
            nonlocal failed
            if Path(destination).name == 'libxess.dll' and not failed:
                failed = True
                raise OSError('injected copy failure')
            return original(source, destination, *args, **kwargs)
        with patch.object(installer, 'atomic_copy', side_effect=fail):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(tree(self.game), self.original)

    def test_first_install_hard_exit_restores_nested_payload_and_reframework_original(self):
        runtime_manifest = json.loads((ROOT / 'defaults/runtime-manifest.json').read_text())
        nested_name = next(name for name in runtime_manifest['files'] if '/' in name)
        nested_original = self.game / nested_name
        nested_original.parent.mkdir(parents=True, exist_ok=True)
        nested_original.write_bytes(b'original nested license')
        nested_original.chmod(0o604)
        ref_original = self.game / constants.REFRAMEWORK_DLL
        ref_original.write_bytes(b'original dinput8')
        ref_original.chmod(0o640)
        reframework_source = self.source / constants.REFRAMEWORK_DLL
        reframework_source.write_bytes(b'new dinput8')
        before = filesystem_state(self.game)
        code = """import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import constants, installer, payload
root = Path(sys.argv[2]); source = Path(sys.argv[3]); target = Path(sys.argv[4])
runtime = json.loads(Path(sys.argv[5]).read_text())['files']
payload._PINNED_FILES = {
 name: {'bytes': (source / name).stat().st_size,
        'sha256': hashlib.sha256((source / name).read_bytes()).hexdigest()}
 for name in runtime
}
constants.RUNTIME_DLL_SHA256 = hashlib.sha256((source / 'OptiScaler.dll').read_bytes()).hexdigest()
original = installer.atomic_copy
def stop_after_dinput8(src, dst, *args, **kwargs):
 result = original(src, dst, *args, **kwargs)
 if Path(dst) == target / constants.REFRAMEWORK_DLL:
  os._exit(86)
 return result
installer.atomic_copy = stop_after_dinput8
installer.install(target, source, reframework_files=[source / constants.REFRAMEWORK_DLL])
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(ROOT),
             str(self.source), str(self.game),
             str(ROOT / 'defaults/runtime-manifest.json')],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 86, result.stderr.decode())
        installer.recover_target_transactions(self.game)
        self.assertEqual(filesystem_state(self.game), before)

    def test_legacy_directory_backup_returns_missing_files_and_preserves_additions(self):
        self.install()
        backup_tree = installer.backup_dir(self.game) / 'Licenses'
        backup_tree.mkdir(parents=True)
        (backup_tree / 'game.txt').write_bytes(b'old game license')
        additions = self.game / 'Licenses' / 'third-party.txt'
        additions.write_bytes(b'new third-party file')
        manifest = installer.read_manifest(self.game)
        manifest['backups']['Licenses'] = 'Licenses'
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps(manifest))

        code = """import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
target = Path(sys.argv[2])
original = installer.atomic_copy
def stop_after_returning_legacy_file(src, dst, *args, **kwargs):
 result = original(src, dst, *args, **kwargs)
 if Path(dst) == target / 'Licenses' / 'game.txt':
  os._exit(87)
 return result
installer.atomic_copy = stop_after_returning_legacy_file
installer.restore_backups(target)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 87, result.stderr.decode())
        journal = next(self.game.glob(installer.TRANSACTION_PREFIX + '*'))
        records = {item['name']: item for item in json.loads(
            (journal / installer.TRANSACTION_RECORD).read_text())['records']}
        self.assertEqual(records['Licenses/game.txt']['kind'], installer.TRANSACTION_FILE)
        self.assertEqual(records['Licenses']['kind'], installer.TRANSACTION_SHARED_DIR)
        installer.recover_target_transactions(self.game)
        self.assertFalse((self.game / 'Licenses' / 'game.txt').exists())
        self.assertEqual((self.game / 'Licenses' / 'third-party.txt').read_bytes(),
                         b'new third-party file')
        installer.restore_backups(self.game)
        self.assertEqual((self.game / 'Licenses' / 'game.txt').read_bytes(),
                         b'old game license')
        self.assertEqual((self.game / 'Licenses' / 'third-party.txt').read_bytes(),
                         b'new third-party file')

    def test_legacy_directory_backup_conflict_keeps_backup_and_refuses_cleanup(self):
        self.install()
        backup_tree = installer.backup_dir(self.game) / 'Licenses'
        backup_tree.mkdir(parents=True)
        (backup_tree / 'game.txt').write_bytes(b'older game license')
        destination = self.game / 'Licenses' / 'game.txt'
        destination.write_bytes(b'later third-party file')
        destination.chmod(0o604)
        manifest = installer.read_manifest(self.game)
        manifest['backups']['Licenses'] = 'Licenses'
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps(manifest))
        before = filesystem_state(self.game)

        with self.assertRaisesRegex(ValueError, 'retained Licenses/game.txt'):
            installer.restore_backups(self.game)

        self.assertEqual(filesystem_state(self.game), before)
        self.assertEqual(destination.read_bytes(), b'later third-party file')
        self.assertEqual((backup_tree / 'game.txt').read_bytes(), b'older game license')

    def test_unmanaged_optiscaler_proxy_is_not_deleted_by_repeated_remove(self):
        proxy = self.game / 'dxgi.dll'
        original = installer.DLL_MARKER + b'hand-installed OptiScaler' + b'\0' * installer.MIN_OPTISCALER_SIZE
        proxy.write_bytes(original)
        proxy.chmod(0o604)
        self.install()
        for _ in range(2):
            installer.uninstall(self.game)
            self.assertEqual(proxy.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(proxy.stat().st_mode), 0o604)

    def test_service_uninstall_reads_game_restore_data_after_transaction_recovery(self):
        self.install()
        game_path = '/games/original'
        original_settings = {'target': None, 'auto': False, 'experiment': True}
        manifest_path = self.game / constants.MANIFEST_NAME
        manifest = installer.read_manifest(self.game)
        manifest.update(game_path=game_path, game_settings_before=original_settings)
        manifest_path.write_text(json.dumps(manifest))
        code = """import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import constants, installer
target = Path(sys.argv[2]); path = target / constants.MANIFEST_NAME
with installer.target_transaction(target, require_stopped=True):
 record = json.loads(path.read_text())
 record['game_settings_before'] = {'target': 'stale', 'auto': True, 'experiment': False}
 installer._atomic_json(path, record)
 os._exit(88)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 88, result.stderr.decode())
        self.assertEqual(installer.read_manifest(self.game)['game_settings_before'],
                         {'target': 'stale', 'auto': True, 'experiment': False})
        with patch.object(self.service.settings, 'restore_game') as restore_game:
            result = asyncio.run(self.service.uninstall(str(self.game)))
        self.assertTrue(result['ok'], result)
        restore_game.assert_called_once_with(game_path, original_settings)

    def test_service_install_checks_game_identity_after_transaction_recovery(self):
        self.install()
        code = """import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import constants, installer
target = Path(sys.argv[2]); path = target / constants.MANIFEST_NAME
with installer.target_transaction(target, require_stopped=True):
 record = json.loads(path.read_text())
 record['game_path'] = '/games/stale'
 installer._atomic_json(path, record)
 os._exit(89)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 89, result.stderr.decode())
        self.service.live_asi_path = lambda: None
        result = asyncio.run(self.service.install(str(self.game), game_path='/games/current'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(installer.read_manifest(self.game)['game_path'], '/games/current')

    def test_failed_proxy_move_can_still_be_removed_without_deleting_game_dll(self):
        self.install()
        before = tree(self.game)
        original = installer.atomic_copy
        failed = False
        def fail(source, destination, *args, **kwargs):
            nonlocal failed
            if Path(destination).name == 'libxess.dll' and not failed:
                failed = True
                raise OSError('injected move failure')
            return original(source, destination, *args, **kwargs)
        with patch.object(installer, 'atomic_copy', side_effect=fail):
            with self.assertRaises(OSError):
                self.install(filename='winmm.dll')
        self.assertEqual(tree(self.game), before)
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)

    def test_incremental_components_remain_owned_through_reinstall(self):
        self.install()
        supplied = self.root / 'supplied'; supplied.mkdir()
        for name in constants.FSR4_SUPPORT_FILES:
            (supplied / name).write_bytes(b'user supplied driver support')
        installer.import_fsr4_files(self.game, supplied)
        patcher = supplied / 'OptiPatcher.asi'; patcher.write_bytes(b'patcher')
        installer.install_plugin_file(self.game, patcher, 'plugins/OptiPatcher.asi')
        self.install()
        manifest = installer.read_manifest(self.game)
        self.assertIn('plugins/OptiPatcher.asi', manifest['files'])
        self.assertIn(constants.FSR4_SUPPORT_FILES[0], manifest['files'])
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)

    def test_service_incremental_patcher_disable_restores_original(self):
        self.install()
        plugins = self.game / 'plugins'; plugins.mkdir(exist_ok=True)
        original = b'pre-existing independent patcher'
        (plugins / 'OptiPatcher.asi').write_bytes(original)
        enabled = asyncio.run(self.service.install_optipatcher(str(self.game), True))
        self.assertTrue(enabled['ok'], enabled)
        disabled = asyncio.run(self.service.install_optipatcher(str(self.game), False))
        self.assertTrue(disabled['ok'], disabled)
        self.assertEqual((plugins / 'OptiPatcher.asi').read_bytes(), original)
        installer.uninstall(self.game)
        self.assertEqual((plugins / 'OptiPatcher.asi').read_bytes(), original)
        self.assertFalse((self.game / constants.BACKUP_DIR).exists())

    def test_restore_point_and_remove_repeat_without_losing_oldest_originals(self):
        for cycle in range(2):
            with self.subTest(cycle=cycle):
                self.install()
                old = installer.read_manifest(self.game); old['optiscaler_version'] = '0.9.4'
                (self.game / constants.MANIFEST_NAME).write_text(json.dumps(old))
                ini = self.game / constants.INI_NAME
                ini.write_bytes(b'; legacy comments\r\n[FSR]\r\nFsr4ForceEnableInt8=true\r\n')
                before = tree(self.game)
                metadata = {'game_path': '/games/PBZ', 'launch_options': 'LSFG=1 gamescope -- %command%',
                            'game_settings': {'target': None, 'auto': False, 'experiment': None}}
                point = restorepoint.create(self.game, metadata)
                self.assertTrue(point['available'])
                # A repeated request must not replace the first restore point.
                restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': 'different'})
                self.install()
                ini.write_text('[FrameGen]\nEnabled=false\n[FSR]\nFsr4ForceEnableInt8=1\n')
                addon = self.root / 'driver.dll'; addon.write_bytes(b'experiment addon')
                installer.install_plugin_file(self.game, addon, 'plugins/OptiPatcher.asi')
                restorepoint.restore(self.game)
                self.assertEqual(tree(self.game), before)
                self.assertEqual(restorepoint.info(self.game)['metadata'], metadata)
                restorepoint.restore(self.game)  # Retry is safe if Steam's setter failed.
                installer.uninstall(self.game)
                self.assertEqual(tree(self.game), self.original)
                self.assertFalse((self.game / restorepoint.RESTORE_DIR).exists())

    def test_remove_upgraded_install_does_not_restore_old_optiscaler(self):
        self.install()
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': '%command%'})
        self.install(filename='winmm.dll')
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)
        self.assertFalse((self.game / restorepoint.RESTORE_DIR).exists())

    def test_known_old_asi_is_removed_for_upgrade_and_restored_on_rollback(self):
        self.install()
        asi = live.asi_path(self.game); asi.parent.mkdir(exist_ok=True)
        content = b'known legacy ASI bytes'; asi.write_bytes(content); asi.chmod(0o600)
        old_mode = stat.S_IMODE(asi.stat().st_mode)
        old = installer.read_manifest(self.game)
        old['optiscaler_version'] = '0.9.4'; old['live_asi'] = True
        old['files'].append(f'{live.PLUGIN_SUBDIR}/{live.ASI_NAME}')
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps(old))
        before = tree(self.game)
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(content).hexdigest()):
            restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': 'LSFG=1 %command%'})
            self.install(live_asi=ROOT / 'bin' / live.ASI_NAME, plugin_root=ROOT)
            packaged = ROOT / 'bin' / live.ASI_NAME
            self.assertEqual(asi.read_bytes(), packaged.read_bytes())
            self.assertTrue(installer.read_manifest(self.game)['live_asi'])
            self.assertEqual(installer.read_manifest(self.game)['live_asi_sha256'],
                             hashlib.sha256(packaged.read_bytes()).hexdigest())
            restorepoint.restore(self.game)
            self.assertEqual(tree(self.game), before)
            self.assertEqual(asi.read_bytes(), content)
            self.assertEqual(stat.S_IMODE(asi.stat().st_mode), old_mode)
            installer.uninstall(self.game)
            self.assertEqual(tree(self.game), self.original)

    def test_direct_restore_refuses_an_unjournaled_ini_alias_before_writing(self):
        self.install()
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''})
        ini = self.game / constants.INI_NAME
        alias = self.game / "OPTISCALER.INI"
        ini.rename(alias)
        before = filesystem_state(self.game)
        with self.assertRaises(ValueError):
            restorepoint.restore(self.game)
        self.assertEqual(filesystem_state(self.game), before)

    def test_restorepoint_refuses_unknown_live_asi_before_any_write(self):
        self.install()
        metadata = {
            'game_path': '/games/test',
            'game_settings': {'target': None, 'auto': None, 'experiment': None},
            'launch_options': '',
        }
        restorepoint.create(self.game, metadata)
        installer.install_live_component(
            self.game, ROOT / 'bin' / live.ASI_NAME, plugin_root=ROOT
        )
        asi = live.asi_path(self.game)
        asi.write_bytes(b'user replacement with unknown ASI bytes')
        asi.chmod(0o601)
        before = filesystem_state(self.game)
        with self.assertRaisesRegex(ValueError, 'unknown bytes'):
            restorepoint.restore(self.game)
        self.assertEqual(filesystem_state(self.game), before)

    def test_restorepoint_allows_known_new_and_legacy_live_asi(self):
        self.install()
        metadata = {
            'game_path': '/games/test',
            'game_settings': {'target': None, 'auto': None, 'experiment': None},
            'launch_options': '',
        }
        baseline = tree(self.game)
        restorepoint.create(self.game, metadata)
        installer.install_live_component(
            self.game, ROOT / 'bin' / live.ASI_NAME, plugin_root=ROOT
        )
        asi = live.asi_path(self.game)
        packaged = (ROOT / 'bin' / live.ASI_NAME).read_bytes()
        self.assertEqual(asi.read_bytes(), packaged)
        restorepoint.restore(self.game)
        self.assertFalse(asi.exists())

        # The old build is also an explicitly recognized rollback source.
        installer.install_live_component(
            self.game, ROOT / 'bin' / live.ASI_NAME, plugin_root=ROOT
        )
        legacy = b'known legacy ASI bytes for rollback'
        asi.write_bytes(legacy)
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(legacy).hexdigest()):
            restorepoint.restore(self.game)
        self.assertFalse(asi.exists())
        self.assertEqual(tree(self.game), baseline)

    def test_unknown_or_unremovable_old_asi_blocks_upgrade_without_file_changes(self):
        self.install()
        asi = live.asi_path(self.game); asi.parent.mkdir(exist_ok=True)
        asi.write_bytes(b'unknown potentially incompatible ASI')
        before = tree(self.game)
        with self.assertRaises((ValueError, RuntimeError)):
            self.install(live_asi=ROOT / 'bin' / live.ASI_NAME, plugin_root=ROOT)
        self.assertEqual(tree(self.game), before)
        known = asi.read_bytes(); real_stash = installer._stash; failed = False
        def refuse(target, source, logger=None):
            nonlocal failed
            if Path(source).resolve() == asi.resolve() and not failed:
                failed = True
                raise PermissionError('injected ASI preservation denial')
            return real_stash(target, source, logger)
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(known).hexdigest()), \
                patch.object(installer, '_stash', new=refuse):
            with self.assertRaises(PermissionError):
                self.install(live_asi=ROOT / 'bin' / live.ASI_NAME, plugin_root=ROOT)
        self.assertEqual(tree(self.game), before)

    def test_failed_rollback_keeps_current_install_and_can_be_retried(self):
        self.install()
        before = tree(self.game)
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': 'LSFG=1 %command%'})
        self.install(filename='winmm.dll')
        current = tree(self.game)
        copy = installer.atomic_copy; failed = False
        def fail_once(source, destination, *args, **kwargs):
            nonlocal failed
            if Path(destination).name == constants.MANIFEST_NAME and not failed:
                failed = True
                raise OSError('injected rollback commit failure')
            return copy(source, destination, *args, **kwargs)
        with patch.object(installer, 'atomic_copy', side_effect=fail_once):
            with self.assertRaises(OSError):
                restorepoint.restore(self.game)
        self.assertTrue(failed, 'fault did not exercise the rollback commit')
        self.assertEqual(tree(self.game), current)
        self.assertTrue(restorepoint.info(self.game)['available'])
        restorepoint.restore(self.game)
        self.assertEqual(tree(self.game), before)

    def test_service_rollback_restores_only_the_related_game_preferences(self):
        self.install()
        game_path = '/games/PBZ'
        self.service.settings.set_target(game_path, 'old target')
        self.service.settings.set_pref('auto_mode:' + game_path, False)
        self.service.settings.set_pref('auto_mode:/games/other', True)
        point = asyncio.run(self.service.create_restore_point(str(self.game), game_path, 'LSFG=1 %command%'))
        self.assertTrue(point['ok'], point)
        self.service.settings.set_target(game_path, 'new target')
        self.service.settings.set_pref('auto_mode:' + game_path, True)
        self.service.settings.set_pref('deck-experiment:' + game_path, True)
        rolled = asyncio.run(self.service.rollback_install(str(self.game), game_path))
        self.assertTrue(rolled['ok'], rolled)
        self.assertEqual(rolled['launch_options'], 'LSFG=1 %command%')
        self.assertEqual(self.service.settings.snapshot_game(game_path),
                         {'target': 'old target', 'auto': False, 'experiment': None})
        self.assertTrue(self.service.settings.get_pref('auto_mode:/games/other'))

    def test_snapshot_restores_originals_first_adopted_after_snapshot(self):
        (self.game / 'winmm.dll').write_bytes(b'original game winmm')
        support_name = constants.FSR4_SUPPORT_FILES[0]
        (self.game / support_name).write_bytes(b'original AMD support')
        self.install()
        before = tree(self.game)
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''})
        self.install(filename='winmm.dll')
        supplied = self.root / 'support'; supplied.mkdir()
        for name in constants.FSR4_SUPPORT_FILES:
            (supplied / name).write_bytes(b'experimental AMD support')
        installer.import_fsr4_files(self.game, supplied)
        for _ in range(2):
            restorepoint.restore(self.game)
            self.assertEqual(tree(self.game), before)
        installer.uninstall(self.game)
        self.assertEqual((self.game / 'winmm.dll').read_bytes(), b'original game winmm')
        self.assertEqual((self.game / support_name).read_bytes(), b'original AMD support')

    def test_switching_proxy_keeps_manual_optiscaler_stashed_until_remove(self):
        manual = installer.DLL_MARKER + b'legacy manual core' + b'\0' * installer.MIN_OPTISCALER_SIZE
        (self.game / 'dxgi.dll').write_bytes(manual)
        self.install()
        self.install(filename='winmm.dll')
        self.assertFalse((self.game / 'dxgi.dll').exists(), 'old core would be loaded alongside the new proxy')
        installer.uninstall(self.game)
        self.assertEqual((self.game / 'dxgi.dll').read_bytes(), manual)

    def test_reinstall_rejects_nested_destination_symlink(self):
        self.install()
        outside = self.root / 'outside.dll'; outside.write_bytes(b'outside sentinel')
        nested = self.game / 'D3D12_Optiscaler/D3D12Core.dll'
        nested.unlink(); nested.symlink_to(outside)
        with self.assertRaises((ValueError, RuntimeError)):
            self.install()
        self.assertEqual(outside.read_bytes(), b'outside sentinel')
        self.assertTrue(nested.is_symlink())

    def test_remove_rejects_file_replaced_with_nonempty_directory(self):
        self.install()
        path = self.game / 'libxess.dll'; path.unlink(); path.mkdir()
        (path / 'user-data').write_bytes(b'keep')
        with self.assertRaises((ValueError, RuntimeError)):
            installer.uninstall(self.game)
        self.assertEqual((path / 'user-data').read_bytes(), b'keep')

    def test_disabling_unowned_component_is_idempotent_and_missing_backup_blocks(self):
        self.install()
        plugins = self.game / 'plugins'; plugins.mkdir(exist_ok=True)
        original = plugins / 'OptiPatcher.asi'; original.write_bytes(b'user patcher')
        for _ in range(2):
            installer.remove_files(self.game, ['plugins/OptiPatcher.asi'])
            self.assertEqual(original.read_bytes(), b'user patcher')
        source = self.root / 'patcher'; source.write_bytes(b'managed patcher')
        installer.install_plugin_file(self.game, source)
        manifest = installer.read_manifest(self.game)
        stored = manifest['backups']['plugins/OptiPatcher.asi']
        (installer.backup_dir(self.game) / stored).unlink()
        before = tree(self.game)
        with self.assertRaises((ValueError, RuntimeError, FileNotFoundError)):
            installer.remove_files(self.game, ['plugins/OptiPatcher.asi'])
        self.assertEqual(tree(self.game), before)

    def test_manual_install_restores_preferences_and_launch_record_atomically(self):
        game = '/games/manual'
        self.service.settings.set_target(game, 'original-target')
        self.service.settings.set_pref('auto_mode:' + game, True)
        result = asyncio.run(self.service.install(str(self.game), game_path=game,
                            launch_options='LSFG=1 %command%', auto_mode=False))
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.service.settings.get_pref('auto_mode:' + game, False))
        self.assertEqual(installer.read_launch_record(self.game)['value'], 'LSFG=1 %command%')
        result = asyncio.run(self.service.uninstall(str(self.game)))
        self.assertTrue(result['ok'], result)
        self.assertEqual(tree(self.game), self.original)
        self.assertEqual(self.service.settings.get_target(game), 'original-target')
        self.assertTrue(self.service.settings.get_pref('auto_mode:' + game))

    def test_failed_first_service_install_leaves_no_launch_record_or_preferences(self):
        game = '/games/failed'
        with patch.object(self.service.settings, 'configure_game', side_effect=OSError('settings disk failure')):
            result = asyncio.run(self.service.install(str(self.game), game_path=game,
                                launch_options='LSFG=1 %command%', auto_mode=False))
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), self.original)
        self.assertEqual(self.service.settings.snapshot_game(game), {'target': None, 'auto': None, 'experiment': None})

    def test_actual_old_runtime_requires_complete_prepared_snapshot(self):
        self.install()
        legacy = installer.DLL_MARKER + b'older core' + b'\0' * installer.MIN_OPTISCALER_SIZE
        (self.game / 'dxgi.dll').write_bytes(legacy)
        self.assertFalse(installer.runtime_current(self.game))
        before = tree(self.game)
        result = asyncio.run(self.service.install(str(self.game)))
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), before)
        point = asyncio.run(self.service.create_restore_point(str(self.game), '/games/PBZ', 'LSFG=1 %command%'))
        self.assertTrue(point['ok'], point)
        result = asyncio.run(self.service.install(str(self.game)))
        self.assertTrue(result['ok'], result)
        rolled = asyncio.run(self.service.rollback_install(str(self.game), '/games/PBZ'))
        self.assertTrue(rolled['ok'], rolled)
        self.assertEqual((self.game / 'dxgi.dll').read_bytes(), legacy)

    def test_interrupted_transaction_recovers_after_process_restart(self):
        ini = self.game / constants.INI_NAME
        ini.write_bytes(b'original before abrupt exit')
        ini.chmod(0o640)
        code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
with installer.target_transaction(sys.argv[2], include_known=False):
 Path(sys.argv[2], 'OptiScaler.ini').write_bytes(b'interrupted partial mutation')
 os._exit(73)
"""
        result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)], capture_output=True)
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        self.assertEqual(ini.read_bytes(), b'interrupted partial mutation')
        recovered = installer.recover_target_transactions(self.game)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(ini.read_bytes(), b'original before abrupt exit')
        self.assertEqual(ini.stat().st_mode & 0o777, 0o640)
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))

    def test_managed_ini_recovers_canonical_absent_and_existing_case_alias(self):
        self.install()
        canonical = self.game / constants.INI_NAME
        alias = self.game / constants.INI_NAME.swapcase()
        canonical.replace(alias)
        alias.chmod(0o604)
        original_ini = alias.read_bytes()
        before = filesystem_state(self.game)
        code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import constants, installer
target = Path(sys.argv[2])
with installer.target_transaction(target,
        extra_names=(constants.INI_NAME, constants.MANIFEST_NAME),
        include_known=False, intent=installer.TRANSACTION_INTENT_MANAGED_INI):
 Path(target, constants.INI_NAME).write_bytes(b'new canonical config')
 os._exit(74)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 74, result.stderr.decode())
        journal = next(self.game.glob(installer.TRANSACTION_PREFIX + '*'))
        record_path = journal / installer.TRANSACTION_RECORD
        record = json.loads(record_path.read_text())
        record['version'] = installer.TRANSACTION_LEGACY_VERSION
        for item in record['records']:
            item.pop('kind', None)
            item.pop('mode', None)
        record_path.write_text(json.dumps(record))
        installer.recover_target_transactions(self.game)
        self.assertEqual(filesystem_state(self.game), before)
        self.assertFalse(canonical.exists())
        self.assertEqual(alias.read_bytes(), original_ini)
        self.assertEqual(stat.S_IMODE(alias.stat().st_mode), 0o604)
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))

    def test_managed_ini_refuses_two_existing_casefold_aliases_before_journaling(self):
        self.install()
        canonical = self.game / constants.INI_NAME
        alias = self.game / constants.INI_NAME.swapcase()
        alias.write_bytes(canonical.read_bytes())
        before = filesystem_state(self.game)
        with self.assertRaisesRegex(ValueError, 'Multiple Wine-equivalent'):
            with installer.target_transaction(
                    self.game, extra_names=(constants.INI_NAME, constants.MANIFEST_NAME),
                    include_known=False,
                    intent=installer.TRANSACTION_INTENT_MANAGED_INI):
                canonical.write_bytes(b'not reached')
        self.assertEqual(filesystem_state(self.game), before)

    def test_live_command_alias_blocks_running_writes_but_allows_stopped_repair(self):
        self.install()
        alias = self.game / live.CMD_FILE.swapcase()
        alias.write_text('old mailbox')
        report = live.status(
            self.game, ROOT / 'bin' / live.ASI_NAME, ROOT,
        )
        self.assertTrue(report['can_install'], report)
        self.assertFalse(report['live_write_safe'], report)
        self.assertIn(live.CMD_FILE.lower(), report.get('live_write_error', '').lower())

    def test_nested_managed_ini_does_not_downgrade_outer_normal_journal(self):
        self.install()
        before = filesystem_state(self.game)
        code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import constants, installer
target = Path(sys.argv[2])
with installer.target_transaction(target):
 with installer.target_transaction(target,
         extra_names=(constants.INI_NAME, constants.MANIFEST_NAME),
         include_known=False, intent=installer.TRANSACTION_INTENT_MANAGED_INI):
  Path(target, constants.INI_NAME).write_bytes(b'nested config change')
  os._exit(75)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 75, result.stderr.decode())
        journal = next(self.game.glob(installer.TRANSACTION_PREFIX + '*'))
        record = json.loads((journal / installer.TRANSACTION_RECORD).read_text())
        self.assertEqual(record['intent'], installer.TRANSACTION_INTENT_NORMAL)
        installer.recover_target_transactions(self.game)
        self.assertEqual(filesystem_state(self.game), before)
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))

    def test_launch_record_failed_publish_can_retry_and_unreadable_is_rejected(self):
        with patch.object(os, 'link', side_effect=OSError('publish denied')):
            with self.assertRaises(OSError):
                installer.record_launch_options(self.game, 'before')
        self.assertFalse(installer.read_launch_record(self.game)['recorded'])
        self.assertTrue(installer.record_launch_options(self.game, 'before')['written'])
        self.assertEqual(installer.record_launch_options(self.game, 'later')['value'], 'before')
        (self.game / constants.LAUNCH_RECORD_NAME).write_bytes(b'\xff')
        with self.assertRaises(ValueError):
            installer.record_launch_options(self.game, 'must not erase unreadable record')

    def test_custom_asi_paths_and_renamed_legacy_asi_block_upgrade(self):
        self.install()
        ini = self.game / constants.INI_NAME
        original = ini.read_bytes()
        for text in ('[Plugins]\nPath=default\n', '[Libraries]\nOptiDllPath=custom\n'):
            ini.write_text(text)
            before = tree(self.game)
            with self.assertRaises((ValueError, RuntimeError)):
                self.install()
            self.assertEqual(tree(self.game), before)
        ini.write_bytes(original)
        plugins = self.game / 'plugins'; plugins.mkdir(exist_ok=True)
        renamed = plugins / 'decky_optiscaler_live-loadlate.ASI'
        renamed.write_bytes(b'known renamed legacy')
        before = tree(self.game)
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(renamed.read_bytes()).hexdigest()):
            with self.assertRaises((ValueError, RuntimeError)):
                self.install()
        self.assertEqual(tree(self.game), before)

    def test_guard_change_preserves_journal_for_later_retry(self):
        ini = self.game / constants.INI_NAME; ini.write_bytes(b'original')
        running = False
        def stopped(_):
            if running:
                raise RuntimeError('game started during mutation')
        with patch.object(installer, 'require_stopped_target', side_effect=stopped):
            with self.assertRaises(RuntimeError):
                with installer.target_transaction(self.game, include_known=False, require_stopped=True):
                    ini.write_bytes(b'partial mutation')
                    running = True
                    raise OSError('injected operation error')
            self.assertTrue(list(self.game.glob('.decky-optiscaler-tx-*')))
            running = False
            installer.recover_target_transactions(self.game)
        self.assertEqual(ini.read_bytes(), b'original')
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))

    def test_interrupted_recovery_restores_exact_files_and_keeps_shared_children(self):
        plugins = self.game / live.PLUGIN_SUBDIR; plugins.mkdir(mode=0o750)
        community = plugins / 'community.asi'; community.write_bytes(b'user original')
        community.chmod(0o640)
        licenses = self.game / 'Licenses'; licenses.mkdir(mode=0o710)
        pinned = licenses / 'XeSS_LICENSE.txt'; pinned.write_bytes(b'game original license')
        pinned.chmod(0o604)
        unrelated = licenses / 'game-license.txt'; unrelated.write_bytes(b'game data')
        code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
target=Path(sys.argv[2])
with installer.target_transaction(target, require_stopped=True):
 Path(target, 'Licenses/XeSS_LICENSE.txt').write_bytes(b'partial managed write')
 Path(target, 'plugins/community.asi').write_bytes(b'user edit')
 Path(target, 'plugins/new.asi').write_bytes(b'third-party addition')
 Path(target, 'Licenses/new-license.txt').write_bytes(b'third-party addition')
 os._exit(81)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 81, result.stderr.decode())
        installer.recover_target_transactions(self.game)
        self.assertEqual(pinned.read_bytes(), b'game original license')
        self.assertEqual(stat.S_IMODE(pinned.stat().st_mode), 0o604)
        self.assertEqual(community.read_bytes(), b'user edit')
        self.assertEqual(stat.S_IMODE(community.stat().st_mode), 0o640)
        self.assertEqual((plugins / 'new.asi').read_bytes(), b'third-party addition')
        self.assertEqual(unrelated.read_bytes(), b'game data')
        self.assertEqual((licenses / 'new-license.txt').read_bytes(), b'third-party addition')
        self.assertEqual(stat.S_IMODE(plugins.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(licenses.stat().st_mode), 0o710)

    def test_recovery_keeps_a_new_nonempty_directory_that_was_absent_at_start(self):
        code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
target=Path(sys.argv[2])
with installer.target_transaction(target, require_stopped=True):
 user=Path(target, 'plugins/community.asi')
 user.parent.mkdir()
 user.write_bytes(b'third-party addition')
 os._exit(82)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 82, result.stderr.decode())
        installer.recover_target_transactions(self.game)
        self.assertEqual((self.game / 'plugins/community.asi').read_bytes(),
                         b'third-party addition')

    def test_unknown_live_asi_blocks_recovery_before_target_or_journal_writes(self):
        code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
target=Path(sys.argv[2])
with installer.target_transaction(target, require_stopped=True):
 Path(target, 'dxgi.dll').write_bytes(b'partial runtime')
 os._exit(83)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 83, result.stderr.decode())
        asi = self.game / live.PLUGIN_SUBDIR / live.ASI_NAME
        asi.parent.mkdir(); asi.write_bytes(b'user replacement with unknown bytes')
        asi.chmod(0o601)
        before = filesystem_state(self.game)
        with self.assertRaisesRegex(ValueError, 'unknown bytes'):
            installer.recover_target_transactions(self.game)
        self.assertEqual(filesystem_state(self.game), before)
        self.assertTrue(list(self.game.glob(installer.TRANSACTION_PREFIX + '*')))

    def test_v1_shared_directory_tree_is_refused_but_v1_file_journal_recovers(self):
        plugins = self.game / live.PLUGIN_SUBDIR; plugins.mkdir()
        (plugins / 'community.asi').write_bytes(b'third-party')
        journal = self.game / f'{installer.TRANSACTION_PREFIX}legacy-shared'
        entries = journal / 'entries'; entries.mkdir(parents=True)
        (entries / '000000').write_bytes(b'original proxy')
        shutil.copytree(plugins, entries / '000001')
        proxy = self.game / 'dxgi.dll'; proxy.write_bytes(b'partial proxy')
        record = {
            'version': installer.TRANSACTION_LEGACY_VERSION,
            'target': str(self.game.resolve()), 'status': 'active',
            'require_stopped': True, 'intent': installer.TRANSACTION_INTENT_STOPPED,
            'records': [
                {'name': 'dxgi.dll', 'exists': True, 'stored': '000000'},
                {'name': 'plugins', 'exists': True, 'stored': '000001'},
            ],
        }
        (journal / installer.TRANSACTION_RECORD).write_text(json.dumps(record))
        before = filesystem_state(self.game)
        with self.assertRaisesRegex(RuntimeError, 'shared directory snapshot is ambiguous'):
            installer.recover_target_transactions(self.game)
        self.assertEqual(filesystem_state(self.game), before)
        self.assertTrue(journal.is_dir())

        shutil.rmtree(journal)
        journal = self.game / f'{installer.TRANSACTION_PREFIX}legacy-absent-shared'
        entries = journal / 'entries'; entries.mkdir(parents=True)
        (entries / '000000').write_bytes(b'original proxy')
        record['records'] = [
            {'name': 'dxgi.dll', 'exists': True, 'stored': '000000'},
            {'name': 'plugins', 'exists': False, 'stored': None},
        ]
        (journal / installer.TRANSACTION_RECORD).write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, 'shared directory snapshot is ambiguous'):
            installer.recover_target_transactions(self.game)
        self.assertEqual((self.game / 'dxgi.dll').read_bytes(), b'partial proxy')
        self.assertTrue(journal.is_dir())

        shutil.rmtree(journal)
        journal = self.game / f'{installer.TRANSACTION_PREFIX}legacy-file'
        entries = journal / 'entries'; entries.mkdir(parents=True)
        (entries / '000000').write_bytes(b'original proxy')
        stash = installer.backup_dir(self.game); stash.mkdir()
        (stash / 'oldest.dll').write_bytes(b'original backup')
        shutil.copytree(stash, entries / '000001')
        (stash / 'partial.dll').write_bytes(b'partial backup write')
        record['records'] = [
            {'name': 'dxgi.dll', 'exists': True, 'stored': '000000'},
            {'name': constants.BACKUP_DIR, 'exists': True, 'stored': '000001'},
        ]
        (journal / installer.TRANSACTION_RECORD).write_text(json.dumps(record))
        installer.recover_target_transactions(self.game)
        self.assertEqual(proxy.read_bytes(), b'original proxy')
        self.assertEqual([item.name for item in stash.iterdir()], ['oldest.dll'])
        self.assertFalse(journal.exists())

    def test_casefold_transaction_alias_records_allow_one_existing_or_both_absent(self):
        canonical = constants.INI_NAME
        alias = canonical.swapcase()
        allowed = (
            [{'name': canonical, 'exists': True}, {'name': alias, 'exists': False}],
            [{'name': canonical, 'exists': False}, {'name': alias, 'exists': True}],
            [{'name': canonical, 'exists': False}, {'name': alias, 'exists': False}],
        )
        for records in allowed:
            installer._validate_casefold_transaction_aliases(records, {canonical})
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            installer._validate_casefold_transaction_aliases(
                [{'name': canonical, 'exists': True}, {'name': alias, 'exists': True}],
                {canonical},
            )

    def test_service_source_preflight_does_not_recover_an_active_transaction(self):
        code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
target=Path(sys.argv[2])
with installer.target_transaction(target, require_stopped=True):
 Path(target, 'dxgi.dll').write_bytes(b'partial proxy')
 os._exit(84)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 84, result.stderr.decode())
        invalid_payload = self.root / 'invalid-payload'; invalid_payload.mkdir()
        self.service.ensure_payload = AsyncMock(return_value=str(invalid_payload))
        result = asyncio.run(self.service.install(str(self.game)))
        self.assertFalse(result['ok'], result)
        self.assertEqual((self.game / 'dxgi.dll').read_bytes(), b'partial proxy')
        self.assertTrue(list(self.game.glob(installer.TRANSACTION_PREFIX + '*')))

    def test_live_status_temp_preflight_uses_one_missing_safe_lstat(self):
        target = self.game.resolve()
        status_temp = target / live.STATUS_TEMP_FILE
        real_lstat = Path.lstat
        calls = []

        def counted(path):
            if Path(path) == status_temp:
                calls.append(path)
            return real_lstat(path)

        with patch.object(Path, 'lstat', new=counted):
            installer._preflight_live_control(target, check_ini_paths=False)
        self.assertEqual(len(calls), 1)

    def test_snapshot_preparation_failure_never_rewrites_target(self):
        ini = self.game / constants.INI_NAME; ini.write_bytes(b'original ini')
        before = tree(self.game)
        identities = {p.name: p.stat().st_ino for p in (ini, self.game / 'dxgi.dll')}
        original_copy = installer._copy_path; count = 0
        def fail_second(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('snapshot disk failure')
            return original_copy(source, destination)
        with patch.object(installer, '_copy_path', side_effect=fail_second):
            with self.assertRaises(OSError):
                with installer.target_transaction(self.game):
                    self.fail('mutation ran before snapshot completed')
        self.assertEqual(tree(self.game), before)
        for name, inode in identities.items():
            self.assertEqual((self.game / name).stat().st_ino, inode)
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))

    def test_original_empty_directories_and_permissions_survive_remove(self):
        plugins = self.game / 'plugins'; plugins.mkdir(mode=0o750)
        licenses = self.game / 'Licenses'; licenses.mkdir(mode=0o700)
        ini = self.game / constants.INI_NAME; ini.write_bytes(b'; original ini')
        ini.chmod(0o640); (self.game / 'dxgi.dll').chmod(0o600)
        self.install()
        installer.uninstall(self.game)
        self.assertTrue(plugins.is_dir()); self.assertEqual(list(plugins.iterdir()), [])
        self.assertTrue(licenses.is_dir()); self.assertEqual(list(licenses.iterdir()), [])
        for path, expected in ((plugins,0o750),(licenses,0o700),(ini,0o640),(self.game/'dxgi.dll',0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, expected, str(path))

    def test_rollback_preserves_untouched_candidate_user_changes(self):
        plugins = self.game / 'plugins'; plugins.mkdir()
        user = plugins / 'OptiPatcher.asi'; user.write_bytes(b'user original')
        self.install()
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''})
        user.write_bytes(b'user changed while experiment was installed')
        self.install(filename='winmm.dll')
        restorepoint.restore(self.game)
        self.assertEqual(user.read_bytes(), b'user changed while experiment was installed')
        installer.uninstall(self.game)
        self.assertEqual(user.read_bytes(), b'user changed while experiment was installed')

    def test_settings_persist_failure_reverts_install_and_both_game_preferences(self):
        game = '/games/persist-failure'
        self.service.settings.set_target(game, 'old target')
        self.service.settings.set_pref('auto_mode:' + game, True)
        original_settings = self.service.settings.path.read_bytes()
        replace = Path.replace; failed = False
        def refuse(path, destination):
            nonlocal failed
            if Path(destination) == self.service.settings.path and not failed:
                failed = True
                raise OSError('injected settings commit failure')
            return replace(path, destination)
        with patch.object(Path, 'replace', new=refuse):
            result = asyncio.run(self.service.install(str(self.game), game_path=game,
                                launch_options='LSFG=1 %command%', auto_mode=False))
        self.assertTrue(failed)
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), self.original)
        self.assertEqual(self.service.settings.get_target(game), 'old target')
        self.assertTrue(self.service.settings.get_pref('auto_mode:' + game))
        self.assertEqual(self.service.settings.path.read_bytes(), original_settings)

    def test_existing_runtime_configuration_stays_in_place_until_remove(self):
        original = b'; user nvngx configuration'
        path = self.game / 'nvngx.ini'; path.write_bytes(original)
        self.install()
        self.assertEqual(path.read_bytes(), original)
        path.write_bytes(b'; changed by game runtime')
        self.install()
        self.assertEqual(path.read_bytes(), b'; changed by game runtime')
        installer.uninstall(self.game)
        self.assertEqual(path.read_bytes(), original)

    def test_null_metadata_or_missing_snapshot_entry_blocks_upgrade(self):
        self.install()
        with self.assertRaises(ValueError):
            restorepoint.create(self.game, {'game_path': None, 'game_settings': None, 'launch_options': None})
        metadata = {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''}
        restorepoint.create(self.game, metadata)
        path = self.game / restorepoint.RESTORE_DIR / restorepoint.SNAPSHOT_FILE
        record = json.loads(path.read_text()); record['entries'].pop()
        path.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            restorepoint.info(self.game)

    def test_unmanaged_incremental_installs_are_refused_without_writes(self):
        source = self.root / 'unmanaged-support'; source.mkdir()
        for name in constants.FSR4_SUPPORT_FILES:
            (source / name).write_bytes(b'new support')
        before = tree(self.game)
        with self.assertRaises((ValueError, RuntimeError)):
            installer.import_fsr4_files(self.game, source)
        self.assertEqual(tree(self.game), before)
        reframework = source / constants.REFRAMEWORK_DLL; reframework.write_bytes(b'new REF')
        with self.assertRaises((ValueError, RuntimeError)):
            installer.add_files(self.game, [reframework])
        self.assertEqual(tree(self.game), before)

    def test_fsr4_import_rejects_empty_or_symlink_source_before_recovery(self):
        code = """import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
target = Path(sys.argv[2])
with installer.target_transaction(target, require_stopped=True):
 Path(target, 'dxgi.dll').write_bytes(b'partial proxy')
 os._exit(90)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 90, result.stderr.decode())
        before = filesystem_state(self.game)
        empty = self.root / 'empty-source'; empty.mkdir()
        symlink = self.root / 'source-link'; symlink.symlink_to(empty, target_is_directory=True)
        for invalid in (empty, symlink):
            with self.subTest(source=invalid.name):
                with self.assertRaises((FileNotFoundError, NotADirectoryError)):
                    installer.import_fsr4_files(self.game, invalid)
                self.assertEqual(filesystem_state(self.game), before)
        self.assertTrue(list(self.game.glob(installer.TRANSACTION_PREFIX + '*')))

    def test_requested_live_sources_are_validated_before_target_recovery(self):
        code = """import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
target = Path(sys.argv[2])
with installer.target_transaction(target, require_stopped=True):
 Path(target, 'dxgi.dll').write_bytes(b'partial proxy')
 os._exit(91)
"""
        result = subprocess.run(
            [sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 91, result.stderr.decode())
        before = filesystem_state(self.game)
        bad_source = self.root / 'bad-live.asi'; bad_source.write_bytes(b'bad live ASI')
        bad_patcher = self.root / 'bad-patcher.asi'; bad_patcher.symlink_to(bad_source)

        unavailable = OptiScalerService(
            self.root / 'corrupt-plugin', self.root / 'bad-settings',
            self.root / 'bad-runtime', self.root / 'bad-home',
            logging.getLogger('bad-source'),
        )
        (unavailable.plugin_dir / 'bin').mkdir(parents=True)
        corrupt_packaged = unavailable.plugin_dir / 'bin' / live.ASI_NAME
        corrupt_packaged.write_bytes(b'corrupt shipped ASI')
        self.assertEqual(unavailable.live_asi_path(), str(corrupt_packaged))

        self.service.live_asi_path = lambda: str(bad_source)
        result = asyncio.run(self.service.install(str(self.game)))
        self.assertFalse(result['ok'], result)
        self.assertEqual(filesystem_state(self.game), before)

        self.service.live_asi_path = lambda: None
        self.service.optipatcher_path = lambda: str(bad_patcher)
        for operation in (
            lambda: self.service.install(str(self.game), optipatcher=True),
            lambda: self.service.install_optipatcher(str(self.game), True),
        ):
            result = asyncio.run(operation())
            self.assertFalse(result['ok'], result)
            self.assertEqual(filesystem_state(self.game), before)
        self.assertTrue(list(self.game.glob(installer.TRANSACTION_PREFIX + '*')))

    def test_rollback_preserves_unowned_files_inside_payload_directories(self):
        self.install()
        licenses = self.game / 'Licenses'
        untouched = licenses / 'user.txt'; untouched.write_bytes(b'before')
        metadata = {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''}
        restorepoint.create(self.game, metadata)
        untouched.write_bytes(b'edited by user')
        added = licenses / 'new-user.txt'; added.write_bytes(b'new user data')
        self.install(filename='winmm.dll')
        restorepoint.restore(self.game)
        self.assertEqual(untouched.read_bytes(), b'edited by user')
        self.assertEqual(added.read_bytes(), b'new user data')

    def test_casefolded_ini_and_plugin_directory_aliases_cannot_load_old_asi(self):
        self.install()
        ini = self.game / constants.INI_NAME; original = ini.read_bytes()
        for text in ('[plugins]\npath=custom\n', '\ufeff[libraries]\noptidllpath=custom\n'):
            ini.write_text(text)
            before = tree(self.game)
            result = asyncio.run(self.service.write_config(str(self.game), [
                {'section': 'FrameGen', 'key': 'Enabled', 'value': 'false'}]))
            self.assertFalse(result['ok'], result)
            self.assertEqual(tree(self.game), before)
        ini.write_bytes(original)
        aliases = self.game / 'Plugins'; aliases.mkdir()
        asi = aliases / live.ASI_NAME; asi.write_bytes(b'legacy bytes')
        before = tree(self.game)
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(asi.read_bytes()).hexdigest()):
            with self.assertRaises((ValueError, RuntimeError)):
                self.install()
        self.assertEqual(tree(self.game), before)

    def test_final_proposed_asi_configuration_is_rejected_before_ini_commit(self):
        self.install()
        before = tree(self.game)
        result = asyncio.run(self.service.write_config(str(self.game), [
            {'section': 'Plugins', 'key': 'Path', 'value': 'custom'}]))
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), before)

    def test_legacy_suffix_backup_is_restored_and_journaled_on_tail_failure(self):
        legacy = self.game / ('dxgi.dll' + installer.LEGACY_BACKUP_SUFFIX)
        legacy.write_bytes(b'oldest legacy original')
        (self.game / 'dxgi.dll').write_bytes(installer.DLL_MARKER + b'\0' * installer.MIN_OPTISCALER_SIZE)
        before = tree(self.game)
        persist = installer._persist_transaction; failed = False
        def refuse(state, target, status='active'):
            nonlocal failed
            if status == 'committed' and not failed:
                failed = True
                raise OSError('injected transaction tail failure')
            return persist(state, target, status)
        with patch.object(installer, '_persist_transaction', side_effect=refuse):
            with self.assertRaises(RuntimeError):
                installer.uninstall(self.game)
        self.assertTrue(failed)
        installer.recover_target_transactions(self.game)
        self.assertEqual(tree(self.game), before)
        installer.uninstall(self.game)
        self.assertEqual((self.game / 'dxgi.dll').read_bytes(), b'oldest legacy original')
        self.assertFalse(legacy.exists())

    def test_waiting_automatic_operations_observe_committed_experiment_mode(self):
        self.install()
        game = '/games/serialized'
        async def verify():
            for operation in ('apply', 'install'):
                self.service.settings.set_pref('deck-experiment:' + game, False)
                ini = IniFile(self.game / constants.INI_NAME)
                ini.set('FrameGen', 'FGInput', 'nofg'); ini.set('FrameGen', 'FGOutput', 'nofg'); ini.save()
                planned = asyncio.Event()
                async def plan(*args):
                    planned.set()
                    return {'recommendation': {}, 'plan': {'available': True, 'filename': 'dxgi.dll',
                        'optipatcher': False, 'settings': [], 'framegen': {'input': 'fsrfg', 'output': 'fsr3'}}}
                self.service.get_auto_plan = AsyncMock(side_effect=plan)
                await self.service._mutation_lock.acquire()
                try:
                    coro = (self.service.apply_auto_settings(str(self.game), game, 'Test') if operation == 'apply'
                            else self.service.auto_install(str(self.game), game, 'Test'))
                    task = asyncio.create_task(coro)
                    await planned.wait()
                    await asyncio.sleep(0)
                    self.service.settings.set_pref('deck-experiment:' + game, True)
                finally:
                    self.service._mutation_lock.release()
                result = await task
                self.assertTrue(result['ok'], result)
                values = IniFile(self.game / constants.INI_NAME)
                self.assertEqual(values.get('FrameGen', 'FGInput'), 'nofg', operation)
                self.assertEqual(values.get('FrameGen', 'FGOutput'), 'nofg', operation)
        asyncio.run(verify())

    def test_interrupted_committed_journal_cleanup_never_blocks_next_operation(self):
        ini = self.game / constants.INI_NAME; ini.write_bytes(b'old value')
        code = """import os,sys,shutil
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from optiscaler import installer
original=shutil.rmtree
def interrupt(path,*args,**kwargs):
 path=Path(path)
 if path.name.startswith('.decky-optiscaler-tx-'):
  (path/installer.TRANSACTION_RECORD).unlink(missing_ok=True)
  os._exit(74)
 return original(path,*args,**kwargs)
shutil.rmtree=interrupt
with installer.target_transaction(sys.argv[2],include_known=False):
 Path(sys.argv[2],'OptiScaler.ini').write_bytes(b'committed new value')
"""
        result = subprocess.run([sys.executable, '-c', code, str(ROOT/'py_modules'), str(self.game)], capture_output=True)
        self.assertEqual(result.returncode,74,result.stderr.decode())
        installer.recover_target_transactions(self.game)
        self.assertEqual(ini.read_bytes(), b'committed new value')
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))
        with installer.target_transaction(self.game, include_known=False):
            ini.write_bytes(b'next operation')
        self.assertEqual(ini.read_bytes(), b'next operation')

    def test_unmanaged_known_runtime_cannot_change_unrecoverable_configuration(self):
        shutil.copy2(self.source / 'OptiScaler.dll', self.game / 'dxgi.dll')
        shutil.copy2(self.source / constants.INI_NAME, self.game / constants.INI_NAME)
        (self.game / constants.LOG_NAME).write_bytes(b'original manual log')
        before = tree(self.game)
        self.assertTrue(installer.runtime_current(self.game))
        self.assertFalse(asyncio.run(self.service.read_config(str(self.game)))['runtime_current'])
        actions = [self.service.write_config(str(self.game), [{'section':'FrameGen','key':'Enabled','value':'false'}]),
                   self.service.reset_config(str(self.game)),
                   self.service.record_launch_options(str(self.game), 'LSFG=1 %command%'),
                   self.service.clear_log(str(self.game))]
        for action in actions:
            result = asyncio.run(action)
            self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), before)

    def test_clear_log_refuses_symlink_and_preserves_outside_file(self):
        self.install()
        outside = self.root / 'outside-log'; outside.write_bytes(b'outside log data')
        log = self.game / constants.LOG_NAME; log.symlink_to(outside)
        result = asyncio.run(self.service.clear_log(str(self.game)))
        self.assertFalse(result['ok'], result)
        self.assertEqual(outside.read_bytes(), b'outside log data')
        self.assertTrue(log.is_symlink())

    def test_first_install_keeps_unowned_directory_children_and_later_edits(self):
        licenses = self.game / 'Licenses'; licenses.mkdir(mode=0o700)
        user = licenses / 'game-license.txt'; user.write_bytes(b'game original license')
        self.install()
        self.assertEqual(user.read_bytes(), b'game original license')
        user.write_bytes(b'game updated license')
        installer.uninstall(self.game)
        self.assertEqual(user.read_bytes(), b'game updated license')
        self.assertEqual(licenses.stat().st_mode & 0o777, 0o700)

    def test_original_added_after_snapshot_is_returned_before_old_backup_restored(self):
        self.install()
        metadata = {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''}
        restorepoint.create(self.game, metadata)
        name = constants.FSR4_SUPPORT_FILES[0]
        target = self.game / name; target.write_bytes(b'new original added by game update')
        source = self.root / 'late-support'; source.mkdir()
        for item in constants.FSR4_SUPPORT_FILES:
            (source / item).write_bytes(b'experimental replacement')
        installer.import_fsr4_files(self.game, source)
        for _ in range(2):
            restorepoint.restore(self.game)
            self.assertEqual(target.read_bytes(), b'new original added by game update')
        installer.uninstall(self.game)
        self.assertEqual(target.read_bytes(), b'new original added by game update')

    def test_f9283_snapshot_restores_new_temp_backup_and_preserves_unknown_temp(self):
        self.install()
        temp_names = {
            live.CMD_TEMP_FILE, live.STATUS_TEMP_FILE, live.LOG_TEMP_FILE,
        }
        old_manifest = installer.read_manifest(self.game)
        old_manifest["files"] = [name for name in old_manifest["files"]
                                  if name not in temp_names]
        for name in temp_names:
            old_manifest["backups"].pop(name, None)
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps(old_manifest))

        # A root-level temp file can predate the newer manifest without having
        # been owned by the old package. The f9283 snapshot did not inventory
        # these three paths, so their later fate must follow current evidence.
        unknown = self.game / live.LOG_TEMP_FILE
        unknown.write_bytes(b"pre-existing unknown temp")
        os.chmod(unknown, 0o604)
        metadata = {
            "game_path": "/games/test",
            "game_settings": {"target": None, "auto": None, "experiment": None},
            "launch_options": "",
        }
        self.assertTrue(restorepoint.create(self.game, metadata)["available"])

        snapshot = self.game / restorepoint.RESTORE_DIR
        record_path = snapshot / restorepoint.SNAPSHOT_FILE
        record = json.loads(record_path.read_text())
        old_candidates = restorepoint._legacy_candidate_paths(
            self.game, legacy_live_inventory=True
        )
        self.assertTrue(temp_names.isdisjoint(old_candidates))
        shutil.rmtree(snapshot / "entries")
        (snapshot / "entries").mkdir()
        legacy_entries = []
        for index, name in enumerate(old_candidates):
            source = installer._safe_path(self.game, name)
            try:
                mode = source.lstat().st_mode
            except FileNotFoundError:
                mode = None
            entry = {"path": name, "exists": mode is not None,
                     "stored": None, "object": None}
            if mode is not None:
                stored = f"{index:06d}"
                destination = snapshot / "entries" / stored
                if stat.S_ISDIR(mode):
                    shutil.copytree(source, destination)
                else:
                    shutil.copy2(source, destination)
                entry["stored"] = stored
                entry["object"] = restorepoint._object_digest(destination)
            legacy_entries.append(entry)
        record["schema_version"] = restorepoint.SNAPSHOT_LEGACY_VERSION
        record["candidate_paths"] = old_candidates
        record["entries"] = legacy_entries
        record["owned"]["files"] = [name for name in record["owned"]["files"]
                                    if name not in temp_names]
        record_path.write_text(json.dumps(record, indent=2))
        self.assertTrue(restorepoint.info(self.game)["available"])

        # The game adds a new original after the old snapshot. Installing the
        # current bridge adopts and backs it up; restoration must return those
        # bytes and permission bits before the old backup tree is swapped in.
        command_temp = self.game / live.CMD_TEMP_FILE
        command_original = b"game-update command temp"
        command_temp.write_bytes(command_original)
        os.chmod(command_temp, 0o620)
        installer.install_live_component(
            self.game, ROOT / "bin" / live.ASI_NAME, plugin_root=ROOT
        )
        third_party_asi = self.game / live.PLUGIN_SUBDIR / "community-added.asi"
        third_party_asi.write_bytes(b"third-party plugin added after snapshot")
        third_party_asi.chmod(0o604)

        # This path is currently owned but has no backup, so it is removed.
        owned_absent = self.game / live.STATUS_TEMP_FILE
        owned_absent.write_bytes(b"new owned temp")
        os.chmod(owned_absent, 0o600)

        # Remove all current evidence for the log temp, then change the file.
        # Restore must preserve this mixed unknown file byte-for-byte.
        current_manifest = installer.read_manifest(self.game)
        current_manifest["files"] = [name for name in current_manifest["files"]
                                      if name != live.LOG_TEMP_FILE]
        current_manifest["backups"].pop(live.LOG_TEMP_FILE, None)
        log_backup = installer.backup_dir(self.game) / live.LOG_TEMP_FILE
        log_backup.unlink(missing_ok=True)
        unknown_bytes = b"changed unknown temp after upgrade"
        unknown.write_bytes(unknown_bytes)
        os.chmod(unknown, 0o604)

        # Simulate the bridge using its temporary command slot after the game
        # original has been stashed.
        command_temp.write_bytes(b"transient live command")
        os.chmod(command_temp, 0o600)
        installer._atomic_json(self.game / constants.MANIFEST_NAME, current_manifest)

        restorepoint.restore(self.game)
        self.assertEqual(command_temp.read_bytes(), command_original)
        self.assertEqual(stat.S_IMODE(command_temp.stat().st_mode), 0o620)
        self.assertEqual(third_party_asi.read_bytes(),
                         b"third-party plugin added after snapshot")
        self.assertEqual(stat.S_IMODE(third_party_asi.stat().st_mode), 0o604)
        self.assertFalse(owned_absent.exists())
        self.assertEqual(unknown.read_bytes(), unknown_bytes)
        self.assertEqual(stat.S_IMODE(unknown.stat().st_mode), 0o604)
        restored_manifest = installer.read_manifest(self.game)
        self.assertTrue(temp_names.isdisjoint(restored_manifest["files"]))
        self.assertTrue(temp_names.isdisjoint(restored_manifest["backups"]))
        installer.uninstall(self.game)
        self.assertEqual(command_temp.read_bytes(), command_original)
        self.assertEqual(third_party_asi.read_bytes(),
                         b"third-party plugin added after snapshot")
        self.assertEqual(unknown.read_bytes(), unknown_bytes)

    def test_rejected_root_proxy_symlink_never_leaves_an_active_journal(self):
        path = self.game / 'dxgi.dll'; path.unlink(); path.symlink_to('user.txt')
        with self.assertRaises((ValueError, RuntimeError)):
            self.install()
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.readlink(), Path('user.txt'))
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))
        installer.recover_target_transactions(self.game)
        path.unlink(); path.write_bytes(self.original['dxgi.dll'])
        self.install()
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)

    def test_real_legacy_manifest_directory_ownership_does_not_backup_its_own_payload(self):
        self.install()
        manifest = installer.read_manifest(self.game)
        manifest['files'] = [name for name in manifest['files'] if '/' not in name]
        manifest.pop('original_directories', None); manifest.pop('created_directories', None)
        manifest['optiscaler_version'] = '0.9.4'
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps(manifest))
        (self.game / 'dxgi.dll').write_bytes(installer.DLL_MARKER + b'old runtime' + b'\0' * installer.MIN_OPTISCALER_SIZE)
        metadata = {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''}
        restorepoint.create(self.game, metadata)
        self.install()
        current = installer.read_manifest(self.game)
        self.assertFalse(any('/' in name for name in current['backups']))
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)
        self.assertFalse((self.game / 'Licenses').exists())
        self.assertFalse((self.game / 'D3D12_Optiscaler').exists())

    def test_preexisting_pinned_directory_file_is_restored_without_touching_other_files(self):
        licenses = self.game / 'Licenses'; licenses.mkdir(mode=0o750)
        pinned = licenses / 'XeSS_LICENSE.txt'; pinned.write_bytes(b'game license original')
        unrelated = licenses / 'game-owned.txt'; unrelated.write_bytes(b'game data')
        self.install()
        self.assertNotEqual(pinned.read_bytes(), b'game license original')
        self.assertEqual(unrelated.read_bytes(), b'game data')
        unrelated.write_bytes(b'game updated data')
        installer.uninstall(self.game)
        self.assertEqual(pinned.read_bytes(), b'game license original')
        self.assertEqual(unrelated.read_bytes(), b'game updated data')
        self.assertEqual(licenses.stat().st_mode & 0o777, 0o750)

    def test_running_target_is_rejected_before_mutation(self):
        before = tree(self.game)
        with patch.object(live, 'status', return_value={'attached': True}):
            with self.assertRaises(RuntimeError):
                self.install()
        self.assertEqual(tree(self.game), before)

    def test_manifest_paths_cannot_delete_a_sibling_or_follow_symlinks(self):
        sentinel = self.root / 'sentinel'; sentinel.write_bytes(b'keep me')
        for name in ('../sentinel', str(sentinel)):
            with self.subTest(name=name):
                (self.game / constants.MANIFEST_NAME).write_text(json.dumps({
                    'plugin': 'decky-optiscaler', 'files': [name], 'dirs': [], 'backups': {}}))
                with self.assertRaises(ValueError):
                    installer.uninstall(self.game)
                self.assertEqual(sentinel.read_bytes(), b'keep me')
        (self.game / constants.MANIFEST_NAME).unlink()
        link = self.game / 'plugins'; link.symlink_to(self.root, target_is_directory=True)
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps({
            'plugin': 'decky-optiscaler', 'files': ['plugins/sentinel'], 'dirs': [], 'backups': {}}))
        with self.assertRaises(ValueError):
            installer.uninstall(self.game)
        self.assertEqual(sentinel.read_bytes(), b'keep me')
        link.unlink()

    def test_automatic_write_failure_is_not_reported_as_success(self):
        planned = {'recommendation': {}, 'plan': {'available': True, 'filename': 'dxgi.dll',
                   'optipatcher': False, 'settings': [{'section': 'Hotfix', 'key': 'DisableOverlays', 'value': 'true'}]}}
        self.service.get_auto_plan = AsyncMock(return_value=planned)
        with patch.object(self.service, '_write_config_sync', return_value={'ok': False, 'error': 'injected INI failure'}):
            result = asyncio.run(self.service.auto_install(str(self.game), '/games/PBZ', 'Phantom Blade Zero'))
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), self.original)
        self.assertFalse(self.service.settings.get_pref('auto_mode:/games/PBZ', False))
        self.install()
        with patch.object(self.service, '_write_config_sync', return_value={'ok': False, 'error': 'injected INI failure'}):
            result = asyncio.run(self.service.apply_auto_settings(str(self.game), '/games/PBZ', 'Phantom Blade Zero'))
        self.assertFalse(result['ok'], result)
        self.assertFalse(self.service.settings.get_pref('auto_mode:/games/PBZ', False))

    def test_runtime_identity_not_manifest_label_controls_new_schema(self):
        self.install()
        self.assertTrue(installer.runtime_current(self.game))
        (self.game / 'dxgi.dll').write_bytes(b'unknown runtime')
        result = asyncio.run(self.service.write_config(str(self.game), [
            {'section': 'FSR', 'key': 'Fsr4ForceEnableInt8', 'value': '1'}]))
        self.assertFalse(result['ok'], result)

    def test_legacy_boolean_migration_changes_only_the_int8_value(self):
        for old, new in [('true', '1'), ('false', '0'), ('auto', 'auto'), ('2', '2')]:
            with self.subTest(value=old):
                text = f'; keep comment\n[FSR]\nFsr4ForceEnableInt8={old}\n[Custom]\nUnrelated=keep\n'
                (self.game / constants.INI_NAME).write_text(text)
                result = asyncio.run(self.service.install(str(self.game)))
                self.assertTrue(result['ok'], result)
                after = (self.game / constants.INI_NAME).read_text()
                self.assertIn('; keep comment', after)
                self.assertIn('Unrelated=keep', after)
                self.assertEqual(IniFile(self.game / constants.INI_NAME).get('FSR', 'Fsr4ForceEnableInt8'), new)
                installer.uninstall(self.game)
                self.assertEqual((self.game / constants.INI_NAME).read_text(), text)
                (self.game / constants.INI_NAME).unlink()

    def test_matching_bridge_install_does_not_trust_a_legacy_heartbeat(self):
        # setUp pins the synthetic core for file-lifecycle checks. This does
        # not simulate native ABI compatibility or create a running session.
        self.install()
        result = asyncio.run(self.service.install_live(str(self.game)))
        self.assertTrue(result['ok'], result)
        self.assertEqual(live.asi_path(self.game).read_bytes(),
                         (ROOT / 'bin' / live.ASI_NAME).read_bytes())
        status = asyncio.run(self.service.get_live_status(str(self.game)))
        self.assertFalse(status['attached'])
        self.assertFalse(status['ready'])
        (self.game / live.STATUS_FILE).write_text('schema 5\nstatus ready\nseq 1\n')
        status = asyncio.run(self.service.get_live_status(str(self.game)))
        self.assertFalse(status['attached'])
        self.assertTrue(status['legacy_running'])
        self.assertFalse(status['ready'])
        self.assertFalse(status['can_switch_upscaler'])
        switched = asyncio.run(self.service.switch_upscaler(str(self.game), 'fsr31'))
        self.assertFalse(switched['ok'], switched)
        written = asyncio.run(self.service.write_config(str(self.game), [
            {'section': 'Menu', 'key': 'ShowFps', 'value': 'true'}]))
        self.assertTrue(written['ok'], written)
        self.assertFalse(written['live']['sent'])
        self.assertFalse(written['live']['confirmed'])
        self.assertFalse((self.game / live.CMD_FILE).exists())
        os.utime(self.game / live.STATUS_FILE, (0, 0))
        stale = asyncio.run(self.service.get_live_status(str(self.game)))
        self.assertFalse(stale['attached'])
        self.assertFalse(stale['legacy_running'])
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)

    def test_pbz_target_and_offline_requirements_are_explicit(self):
        root = self.root / 'PBZ'; canonical = root / 'platform/steam'
        canonical.mkdir(parents=True); (canonical / 'PBZ.exe').write_bytes(b'MZ')
        decoy = root / 'ProjectPBZ/Binaries/Win64'; decoy.mkdir(parents=True)
        (decoy / 'PBZ-Win64-Shipping.exe').write_bytes(b'MZ' + b'\0' * 1024)
        detail = asyncio.run(self.service.get_game(str(root), 'Phantom Blade Zero'))
        self.assertEqual(detail['target'], str(canonical))
        recommendation = gameprofiles.recommendation({'matched': False}, '影之刃零')
        plan = gameprofiles.constrain({}, recommendation)
        self.assertTrue(plan['runtime_supported'])
        self.assertTrue(any('100%' in step for step in plan['manual_steps']))
        with patch.object(gameprofiles, 'OPTISCALER_VERSION', '0.9.4'):
            self.assertFalse(gameprofiles.constrain({}, recommendation)['runtime_supported'])

    def test_pinned_pbz_identity_resolves_renamed_root_for_detail_and_automatic_setup(self):
        root = self.root / 'My game'
        canonical = root / 'platform/steam'
        canonical.mkdir(parents=True)
        (canonical / 'PBZ.exe').write_bytes(b'MZ')
        # This higher ranked candidate has no OptiScaler install. A PBZ pin
        # should make the canonical folder the initial setup target.
        decoy = root / 'OtherGame/Binaries/Win64'
        decoy.mkdir(parents=True)
        (decoy / 'OtherGame-Win64-Shipping.exe').write_bytes(b'MZ')
        self.service.settings.set_wiki_entry(str(root), 'Phantom Blade Zero')

        recommendation = {'matched': True, 'game': 'Phantom Blade Zero',
                          'filename': 'dxgi.dll', 'filename_source': 'wiki entry',
                          'detail': {}, 'compatibility': '✅'}
        base_plan = {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                     'settings': []}
        original_detect = installer.detect

        def detect_with_decoy_install(path):
            if Path(path) == decoy:
                return {'path': str(decoy), 'installed': True, 'filename': 'dxgi.dll'}
            return original_detect(path)

        detail = asyncio.run(self.service.get_game(str(root), 'My Steam shortcut'))
        with patch.object(installer, 'detect', side_effect=detect_with_decoy_install):
            existing_wrong_folder = asyncio.run(
                self.service.get_game(str(root), 'My Steam shortcut'))
        with patch.object(self.service.wiki, 'entry_by_name', return_value={'name': 'Phantom Blade Zero'}), \
             patch.object(self.service.wiki, 'recommend_entry', return_value=recommendation), \
             patch('optiscaler.service.autoplan.build', return_value=base_plan):
            planned = asyncio.run(self.service.get_auto_plan('My Steam shortcut', game_path=str(root)))

        self.assertEqual(detail['pbz_target'], str(canonical))
        self.assertEqual(detail['target'], str(canonical))
        self.assertTrue(detail['pbz_identity'])
        self.assertEqual(existing_wrong_folder['pbz_target'], str(canonical))
        self.assertEqual(existing_wrong_folder['target'], str(decoy))
        self.assertEqual(planned['recommendation']['game'], 'Phantom Blade Zero')
        self.assertEqual(planned['plan']['required_target'], 'PBZ/platform/steam')

        # The mock below exercises the selected target route only. Keep a real
        # managed install underneath automatic config preflight.
        installer.install(canonical, self.source)
        self.service.get_auto_plan = AsyncMock(return_value=planned)
        self.service.install = AsyncMock(return_value={'ok': True, 'applied': []})
        wrong_target = self.root / 'wrong-target'; wrong_target.mkdir()
        rejected_install = asyncio.run(self.service.auto_install(
            str(wrong_target), str(root), 'My Steam shortcut'))
        self.assertFalse(rejected_install['ok'])
        self.service.install.assert_not_awaited()

        installed = asyncio.run(self.service.auto_install(
            str(canonical), str(root), 'My Steam shortcut'))
        self.assertTrue(installed['ok'], installed)
        self.service.install.assert_awaited_once()

        self.service._write_config_sync = lambda target, changes: {
            'ok': True, 'applied': [], 'rejected': []}
        with patch('optiscaler.service.autoplan.setting_changes', return_value=[]), \
             patch('optiscaler.service.autoplan.framegen_changes', return_value=[]):
            rejected_settings = asyncio.run(self.service.apply_auto_settings(
                str(wrong_target), str(root), 'My Steam shortcut'))
            self.assertFalse(rejected_settings['ok'])
            applied_settings = asyncio.run(self.service.apply_auto_settings(
                str(canonical), str(root), 'My Steam shortcut'))
        self.assertTrue(applied_settings['ok'], applied_settings)

    def test_pbz_root_basename_can_supply_recommendation_identity(self):
        root = self.root / 'PBZ'
        canonical = root / 'platform/steam'
        canonical.mkdir(parents=True)
        (canonical / 'PBZ.exe').write_bytes(b'MZ')
        empty = {'matched': False, 'searched': [], 'entry_count': 0,
                 'list_available': False, 'near_misses': [], 'game': None,
                 'page': None, 'filename': 'dxgi.dll', 'filename_source': 'default',
                 'alternatives': [], 'compatibility': None, 'inputs': None,
                 'notes': None, 'optipatcher': False, 'wiki_url': None,
                 'detail': {}, 'detail_pending': False, 'list_meta': {},
                 'match_score': None}
        empty.update(matched=True, game='Unrelated Game', filename='winmm.dll',
                     alternatives=['version.dll'], optipatcher=True,
                     detail={'Settings': 'UnrelatedSetting=true'})
        base_plan = {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                     'settings': []}
        with patch.object(self.service.wiki, 'recommend', return_value=empty), \
             patch('optiscaler.service.autoplan.build', return_value=base_plan):
            planned = asyncio.run(self.service.get_auto_plan(
                'My renamed shortcut', game_path=str(root)))
        self.assertTrue(planned['recommendation']['required_target'])
        self.assertEqual(planned['plan']['required_target'], 'PBZ/platform/steam')
        self.assertEqual(planned['recommendation']['alternatives'], [])
        self.assertFalse(planned['recommendation']['optipatcher'])
        self.assertNotIn('Settings', planned['recommendation']['detail'])

    def test_pbz_identity_requires_an_exact_known_title(self):
        root = self.root / 'renamed'; canonical = root / 'platform/steam'
        canonical.mkdir(parents=True); (canonical / 'PBZ.exe').write_bytes(b'MZ')
        self.assertIsNone(gameprofiles.target_for(
            root, 'Phantom Blade Zero fan game', 'My PBZ collection'))
        unrelated = gameprofiles.recommendation(
            {'matched': True, 'game': 'My PBZ collection'}, 'My renamed title')
        plan = gameprofiles.constrain({}, unrelated)
        self.assertNotIn('required_target', plan)
        self.assertFalse(unrelated.get('required_target'))

    def test_fuzzy_pbz_wiki_match_does_not_grant_pbz_profile_or_target_identity(self):
        root = self.root / 'Phantom Blade Zero fan game'
        canonical = root / 'platform/steam'
        canonical.mkdir(parents=True)
        (canonical / 'PBZ.exe').write_bytes(b'MZ')
        entries = [{'name': 'Phantom Blade Zero', 'key': 'phantombladezero',
                    'page': None, 'compatibility': '✅', 'inputs': 'DLSSG via SL',
                    'optipatcher': False, 'notes': ''}]
        base_plan = {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                     'settings': []}
        with patch.object(self.service.wiki, 'load_entries',
                          return_value=(entries, {'source': 'test-cache'})), \
             patch('optiscaler.service.autoplan.build', return_value=base_plan):
            planned = asyncio.run(self.service.get_auto_plan(
                'Phantom Blade Zero fan game', game_path=str(root)))

        detail = asyncio.run(self.service.get_game(str(root), 'Phantom Blade Zero fan game'))
        self.assertEqual(planned['recommendation']['game'], 'Phantom Blade Zero')
        self.assertGreater(planned['recommendation']['match_score'], 0.78)
        self.assertNotIn('required_target', planned['recommendation'])
        self.assertNotIn('required_target', planned['plan'])
        self.assertFalse(detail['pbz_identity'])
        self.assertIsNone(detail['pbz_target'])

    def test_missing_pinned_pbz_row_uses_offline_profile_without_unrelated_match(self):
        root = self.root / 'Renamed shortcut'
        self.service.settings.set_wiki_entry(str(root), 'Phantom Blade Zero')
        empty = {'matched': False, 'searched': [], 'entry_count': 2,
                 'list_available': True, 'near_misses': [], 'game': None,
                 'page': None, 'filename': 'dxgi.dll', 'filename_source': 'default',
                 'alternatives': [], 'compatibility': None, 'inputs': None,
                 'notes': None, 'optipatcher': False, 'wiki_url': None,
                 'detail': {}, 'detail_pending': False, 'list_meta': {'source': 'cache'},
                 'match_score': None}
        unrelated = {**empty, 'matched': True, 'game': 'Phantom Blade Zero fan game',
                     'detail': {'Notes': 'unrelated settings'}, 'filename': 'winmm.dll'}

        def recommend(query, extra_names=None, force=False):
            return empty if not query else unrelated

        base_plan = {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                     'settings': []}
        with patch.object(self.service.wiki, 'entry_by_name', return_value=None), \
             patch.object(self.service.wiki, 'recommend', side_effect=recommend) as recommend_mock, \
             patch('optiscaler.service.autoplan.build', return_value=base_plan):
            planned = asyncio.run(self.service.get_auto_plan(
                'My renamed Steam shortcut', game_path=str(root)))

        recommend_mock.assert_called_once_with('', [], False)
        recommendation = planned['recommendation']
        self.assertTrue(recommendation['matched'])
        self.assertEqual(recommendation['game'], 'Phantom Blade Zero')
        self.assertEqual(recommendation['filename_source'], 'reviewed wiki profile')
        self.assertEqual(recommendation['detail']['Notes'],
                         'Keep Super Resolution Output Scale at 100%; only change the upscaler preset.')
        self.assertNotEqual(recommendation['detail'].get('Notes'), 'unrelated settings')
        self.assertEqual(recommendation['searched'], ['My renamed Steam shortcut'])
        self.assertEqual(planned['plan']['required_target'], 'PBZ/platform/steam')

    def test_generic_automatic_setup_and_settings_keep_their_existing_path_rules(self):
        root = self.root / 'Generic Game'
        target = root / 'platform/steam'
        target.mkdir(parents=True)
        (target / 'PBZ.exe').write_bytes(b'MZ')
        self.service.settings.set_wiki_entry(str(root), 'Unrelated Compatibility Entry')
        detail = asyncio.run(self.service.get_game(str(root), 'Generic Game'))
        self.assertIsNone(detail['pbz_target'])
        # Automatic settings require a manifest-backed config recovery path;
        # the service.install mock below only isolates target routing.
        installer.install(target, self.source)

        planned = {'recommendation': {'matched': True, 'game': 'Generic Game'},
                   'plan': {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                            'settings': []}}
        self.service.get_auto_plan = AsyncMock(return_value=planned)
        self.service.install = AsyncMock(return_value={'ok': True, 'applied': []})
        other_pbz_path = str(self.root / 'Other PBZ game')
        self.service.settings.set_pref('deck-experiment:' + other_pbz_path, True)
        frame_changes = [{'section': 'FrameGen', 'key': 'FGInput', 'value': 'fsrfg'}]
        written = []

        def record_write(directory, changes):
            written.extend(changes)
            return {'ok': True, 'applied': list(changes), 'rejected': []}

        self.service._write_config_sync = record_write
        with patch('optiscaler.service.autoplan.setting_changes', return_value=[]), \
             patch('optiscaler.service.autoplan.framegen_changes', return_value=frame_changes):
            installed = asyncio.run(self.service.auto_install(
                str(target), str(root), 'Generic Game'))
            configured = asyncio.run(self.service.apply_auto_settings(
                str(target), str(root), 'Generic Game'))
        self.assertTrue(installed['ok'], installed)
        self.assertTrue(configured['ok'], configured)
        self.assertEqual(written, frame_changes)


class PayloadCompletionTests(unittest.TestCase):
    def test_committed_cache_rejects_extra_nested_files_and_null_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = payload.Payload(Path(tmp) / 'payload.7z', Path(tmp) / 'cache')
            item.root.mkdir(parents=True)
            real = json.loads((ROOT / 'defaults/runtime-manifest.json').read_text())['files']
            pinned = {}
            for name in real:
                path = item.root / name; path.parent.mkdir(parents=True, exist_ok=True)
                data = ('fixture ' + name).encode(); path.write_bytes(data)
                pinned[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            with patch.object(payload, '_PINNED_FILES', pinned):
                inventory = payload._inventory_state(item.root)
                self.assertIsNotNone(inventory)
                marker = item.root / payload.COMMIT_MARKER
                record = {'version': constants.OPTISCALER_VERSION,
                          'archive_sha256': constants.PAYLOAD_SHA256, 'inventory': inventory}
                marker.write_text(json.dumps(record))
                self.assertTrue(item.ready)
                extra = item.root / 'D3D12_Optiscaler/unpinned.dll'; extra.write_bytes(b'extra')
                self.assertFalse(item.ready)
                extra.unlink()
                (item.root / 'D3D12_Optiscaler/D3D12Core.dll').unlink()
                for invalid in (None, {}, [], 'inventory'):
                    record['inventory'] = invalid; marker.write_text(json.dumps(record))
                    self.assertFalse(item.ready)

    def test_a_marker_without_the_required_inventory_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'payload.7z'; archive.write_bytes(b'archive')
            item = payload.Payload(archive, Path(tmp) / 'cache')
            item.root.mkdir(parents=True)
            (item.root / 'OptiScaler.dll').write_bytes(b'partial extraction')
            self.assertFalse(item.ready)


if __name__ == '__main__':
    unittest.main()
