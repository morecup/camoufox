"""
Generates per-context + global fingerprint configs using the Camoufox Python API.
Called by the camoufox-tester to get realistic fingerprint data.

Output: JSON object to stdout with macPerContext, linuxPerContext, macGlobal, linuxGlobal
"""
import json
import sys
from pathlib import Path
from typing import Any, List


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHONLIB_DIR = REPO_ROOT / 'pythonlib'


def _load_generate_context_fingerprint():
    try:
        from camoufox.fingerprints import generate_context_fingerprint
    except ImportError:
        if PYTHONLIB_DIR.exists():
            sys.path.insert(0, str(PYTHONLIB_DIR))
            try:
                from camoufox.fingerprints import generate_context_fingerprint
            except ImportError:
                pass
            else:
                return generate_context_fingerprint
        print(
            'ERROR: camoufox Python package not installed.\n'
            '  Run: pip install camoufox  (or: bash scripts/setup.sh)',
            file=sys.stderr,
        )
        if PYTHONLIB_DIR.exists():
            print(f'  Source fallback tried: {PYTHONLIB_DIR}', file=sys.stderr)
        sys.exit(1)
    return generate_context_fingerprint


def voice_names(voices: List[Any]) -> List[str]:
    names: List[str] = []
    for voice in voices or []:
        if isinstance(voice, str) and voice:
            names.append(voice)
        elif isinstance(voice, dict):
            name = voice.get('name')
            if isinstance(name, str) and name:
                names.append(name)
    return names


def convert_preset(ctx):
    """Convert a generate_context_fingerprint() result to camelCase for TypeScript."""
    preset = ctx['preset']
    config = ctx['config']
    nav = preset.get('navigator', {})
    screen = preset.get('screen', {})
    webgl = preset.get('webgl', {})

    return {
        'initScript': ctx['init_script'],
        'contextOptions': {
            'userAgent': ctx['context_options'].get('user_agent'),
            'viewport': ctx['context_options'].get('viewport'),
            'deviceScaleFactor': ctx['context_options'].get('device_scale_factor'),
            'locale': ctx['context_options'].get('locale'),
            'timezoneId': ctx['context_options'].get('timezone_id'),
        },
        'camouConfig': config,
        'profileConfig': {
            'fontSpacingSeed': config.get('fonts:spacing_seed', 0),
            'audioSeed': config.get('audio:seed', 0),
            'canvasSeed': config.get('canvas:seed', 0),
            'screenWidth': screen.get('width', 1920),
            'screenHeight': screen.get('height', 1080),
            'screenColorDepth': screen.get('colorDepth', 24),
            'navigatorPlatform': nav.get('platform', ''),
            'navigatorOscpu': config.get('navigator.oscpu', ''),
            'navigatorUserAgent': config.get('navigator.userAgent', ''),
            'hardwareConcurrency': nav.get('hardwareConcurrency', 0),
            'webglVendor': webgl.get('unmaskedVendor', ''),
            'webglRenderer': webgl.get('unmaskedRenderer', ''),
            'timezone': config.get('timezone', preset.get('timezone', '')),
            'fontList': config.get('fonts', preset.get('fonts', [])),
            'speechVoices': voice_names(config.get('voices', preset.get('speechVoices', []))),
        },
    }


def screen_key(preset):
    pc = preset['profileConfig']
    return (pc.get('screenWidth', 0), pc.get('screenHeight', 0))


def generate_unique_screen_presets(os_name, count):
    presets = []
    seen_screens = set()
    attempts = 0
    max_attempts = max(count * 20, count)

    while len(presets) < count and attempts < max_attempts:
        attempts += 1
        preset = convert_preset(generate_context_fingerprint(os=os_name))
        current_screen = screen_key(preset)
        if current_screen in seen_screens:
            continue
        seen_screens.add(current_screen)
        presets.append(preset)

    while len(presets) < count:
        presets.append(convert_preset(generate_context_fingerprint(os=os_name)))

    return presets


def main():
    generate_context_fingerprint = _load_generate_context_fingerprint()
    results = {
        'macPerContext': [],
        'linuxPerContext': [],
        'macGlobal': None,
        'linuxGlobal': None,
    }

    # 3 macOS per-context profiles
    results['macPerContext'] = generate_unique_screen_presets('macos', 3)

    # 3 Linux per-context profiles
    results['linuxPerContext'] = generate_unique_screen_presets('linux', 3)

    # 1 macOS global profile
    ctx = generate_context_fingerprint(os='macos')
    results['macGlobal'] = convert_preset(ctx)

    # 1 Linux global profile
    ctx = generate_context_fingerprint(os='linux')
    results['linuxGlobal'] = convert_preset(ctx)

    json.dump(results, sys.stdout)


if __name__ == '__main__':
    main()
