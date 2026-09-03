#!/usr/bin/env python3
"""One-shot: open the native image-captioning gate via HiveConfiguration.

WHY this script exists (read before changing it)
------------------------------------------------
Stock OpenMoxie does NOT set data_sharing or the three IC props in
DEFAULT_ROBOT_CONFIG / DEFAULT_ROBOT_SETTINGS (site/hive/mqtt/robot_data.py).
The owner's core-patched tree currently writes those into the Python defaults;
this script is the additive substitute that does the same thing by editing the
HiveConfiguration DB row named 'default' — no source edit of robot_data.py.

CRITICAL: build_config() in robot_data.py uses hive_cfg.common_config and
hive_cfg.common_settings WHOLESALE when the row has them set — it does NOT
merge the row with DEFAULT_ROBOT_CONFIG / DEFAULT_ROBOT_SETTINGS:

    base_cfg = (hive_cfg.common_config if hive_cfg and hive_cfg.common_config
                else DEFAULT_ROBOT_CONFIG).copy()
    base_cfg["settings"] = hive_cfg.common_settings if hive_cfg and hive_cfg.common_settings
                           else DEFAULT_ROBOT_SETTINGS

So this script MUST merge against the EXISTING DB row. Writing a minimal
{props: {image_captioning, ic_by_rb, gcp_upload_disable}} dict would silently
wipe every other settings prop already configured in that row (and, if the
row was empty, would also displace the in-code DEFAULT_ROBOT_* values that
build_config currently falls back to). When the row is null/empty we seed
from DEFAULT_ROBOT_* (read-only import of core, never an edit) so the first
write does not drop stock pairing/volume/wake/stt props.

Values are strings ("1"/"0"), matching the rest of this codebase — never
JSON booleans/ints.

Idempotent: a second run prints the same before/after and says so, no write
if nothing changed.

Run (from repo root, documented equivalent of DJANGO_SETTINGS_MODULE=hive.settings
-- this project's settings module is openmoxie.settings, copied from site/load_see.py):

    DJANGO_SETTINGS_MODULE=openmoxie.settings \\
      /path/to/openmoxie/venv/bin/python vision-sidecar/apply_vision_config.py \\
      --site-dir /path/to/openmoxie/site

See: docs/ENABLE_VISION_ON_MOXIE.md, site/hive/mqtt/robot_data.py build_config()
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys

VISION_CONFIG_KEYS = {'data_sharing': 'full'}
VISION_PROP_KEYS = {
    'image_captioning': '1',
    'ic_by_rb': '1',
    'gcp_upload_disable': '0',
}


def merge_vision_config(common_config, common_settings):
    """Additive merge of vision keys. Never drops unrelated existing keys.

    Returns (new_config, new_settings).

    - common_config: dict or null-like -> dict with data_sharing='full'
    - common_settings: dict or null-like -> dict whose props dict has the
      three IC string flags. If props is missing, create {}. If props exists,
      only the three keys are written; every other props key is preserved.

    Caller is responsible for seeding DEFAULT_ROBOT_* when the DB row was
    empty; this function just merges onto whatever it is given.
    """
    if isinstance(common_config, dict):
        new_config = copy.deepcopy(common_config)
    else:
        new_config = {}
    if isinstance(common_settings, dict):
        new_settings = copy.deepcopy(common_settings)
    else:
        new_settings = {}

    for key, value in VISION_CONFIG_KEYS.items():
        new_config[key] = value

    props = new_settings.get('props')
    if not isinstance(props, dict):
        # Non-dict props cannot receive keyed flags; start a fresh props dict.
        # (A non-dict here is already unusable by the robot.)
        new_settings['props'] = {}
        props = new_settings['props']
    for key, value in VISION_PROP_KEYS.items():
        props[key] = value

    return new_config, new_settings


def _pretty(obj):
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


def _parse_args(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description='Merge vision keys into HiveConfiguration(name=default), additively.'
    )
    parser.add_argument(
        '--site-dir',
        default=os.path.join(here, '..', 'site'),
        help='OpenMoxie site/ directory (added to sys.path so openmoxie.settings imports).',
    )
    return parser.parse_args(argv)


def _seed_if_empty(raw_config, raw_settings):
    """If the DB field is empty, seed from in-code defaults so wholesale-replace is safe."""
    seeded_config, seeded_settings = raw_config, raw_settings
    notes = []
    if not isinstance(raw_config, dict) or not raw_config:
        try:
            from hive.mqtt.robot_data import DEFAULT_ROBOT_CONFIG

            seeded_config = copy.deepcopy(DEFAULT_ROBOT_CONFIG)
            notes.append(
                'NOTE: common_config was empty; seeded from DEFAULT_ROBOT_CONFIG before merge '
                '(build_config uses this row WHOLESALE — writing vision keys alone would drop '
                'stock pairing/volume/timezone fields).'
            )
        except Exception as exc:
            seeded_config = {} if not isinstance(raw_config, dict) else copy.deepcopy(raw_config)
            notes.append(
                f'NOTE: common_config empty and DEFAULT_ROBOT_CONFIG unavailable ({exc}); '
                'starting from {}.'
            )
    if not isinstance(raw_settings, dict) or not raw_settings:
        try:
            from hive.mqtt.robot_data import DEFAULT_ROBOT_SETTINGS

            seeded_settings = copy.deepcopy(DEFAULT_ROBOT_SETTINGS)
            notes.append(
                'NOTE: common_settings was empty; seeded from DEFAULT_ROBOT_SETTINGS before merge '
                '(build_config uses this row WHOLESALE — writing vision props alone would drop '
                'stock wake/stt/touch props).'
            )
        except Exception as exc:
            seeded_settings = {} if not isinstance(raw_settings, dict) else copy.deepcopy(raw_settings)
            notes.append(
                f'NOTE: common_settings empty and DEFAULT_ROBOT_SETTINGS unavailable ({exc}); '
                'starting from {}.'
            )
    return seeded_config, seeded_settings, notes


def main(argv=None):
    args = _parse_args(argv)
    site_dir = os.path.abspath(args.site_dir)
    if site_dir not in sys.path:
        sys.path.insert(0, site_dir)

    # Mirror site/load_see.py lines 17-19 (after putting site/ on sys.path,
    # because this file lives outside site/). `os` must NOT be re-imported
    # here: a local import makes `os` function-local for the whole body, so
    # the os.path.abspath() above raises UnboundLocalError (issue #2).
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'openmoxie.settings')
    django.setup()

    from hive.models import HiveConfiguration  # noqa: E402

    cfg, created = HiveConfiguration.objects.get_or_create(name='default')
    print(f"HiveConfiguration name='default' created={created} pk={cfg.pk}")

    before_config = copy.deepcopy(cfg.common_config)
    before_settings = copy.deepcopy(cfg.common_settings)
    print('--- BEFORE common_config ---')
    print(_pretty(before_config))
    print('--- BEFORE common_settings ---')
    print(_pretty(before_settings))

    seeded_config, seeded_settings, notes = _seed_if_empty(before_config, before_settings)
    for note in notes:
        print(note)

    new_config, new_settings = merge_vision_config(seeded_config, seeded_settings)

    # Compare against the actual DB values (not the seed) so a row that already
    # equals the merged result is reported as unchanged. None != dict counts
    # as a change even if the dict is empty.
    changed = (new_config != before_config) or (new_settings != before_settings)

    print('--- AFTER common_config ---')
    print(_pretty(new_config))
    print('--- AFTER common_settings ---')
    print(_pretty(new_settings))

    if not changed:
        print('Unchanged (idempotent). No save.')
        return 0

    cfg.common_config = new_config
    cfg.common_settings = new_settings
    cfg.save()
    print('Saved HiveConfiguration name=\'default\'.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
