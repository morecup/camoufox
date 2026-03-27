"""
Generates per-context + global fingerprint configs using the Camoufox Python API.
Called by the camoufox-tester to get realistic fingerprint data.

Output: JSON object to stdout with macPerContext, linuxPerContext, macGlobal, linuxGlobal
"""
import json
import sys
from pathlib import Path


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
            'speechVoices': config.get('voices', preset.get('speechVoices', [])),
        },
    }


def main():
    generate_context_fingerprint = _load_generate_context_fingerprint()
    results = {
        'macPerContext': [],
        'linuxPerContext': [],
        'macGlobal': None,
        'linuxGlobal': None,
    }

    # 3 macOS per-context profiles
    for _ in range(3):
        ctx = generate_context_fingerprint(os='macos')
        results['macPerContext'].append(convert_preset(ctx))

    # 3 Linux per-context profiles
    for _ in range(3):
        ctx = generate_context_fingerprint(os='linux')
        results['linuxPerContext'].append(convert_preset(ctx))

    # 1 macOS global profile
    ctx = generate_context_fingerprint(os='macos')
    results['macGlobal'] = convert_preset(ctx)

    # 1 Linux global profile
    ctx = generate_context_fingerprint(os='linux')
    results['linuxGlobal'] = convert_preset(ctx)

    json.dump(results, sys.stdout)


if __name__ == '__main__':
    main()
