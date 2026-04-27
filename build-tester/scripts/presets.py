"""
Fingerprint preset generation, injection, and profile config conversion.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, List

from constants import TEST_TIMEZONES, WEBRTC_TEST_IP

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHONLIB_DIR = REPO_ROOT / "pythonlib"


def _voice_names(voices: List[Any]) -> List[str]:
    names: List[str] = []
    for voice in voices or []:
        if isinstance(voice, str) and voice:
            names.append(voice)
        elif isinstance(voice, dict):
            name = voice.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _ensure_speech_voices_init_script(init_script: str) -> str:
    if "setSpeechVoices" in init_script:
        return init_script
    inject = '  if (typeof w.setSpeechVoices === "function") w.setSpeechVoices("");\n'
    if init_script.endswith("})();"):
        return init_script[:-5] + inject + "})();"
    return init_script + "\n" + inject


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
            "ERROR: camoufox Python package not installed.\n"
            "  Run: pip install camoufox  (or: bash scripts/setup.sh)",
            file=sys.stderr,
        )
        if PYTHONLIB_DIR.exists():
            print(f"  Source fallback tried: {PYTHONLIB_DIR}", file=sys.stderr)
        sys.exit(1)
    return generate_context_fingerprint


# ─── Preset Generation ────────────────────────────────────────────────────────

def convert_preset(ctx: dict) -> dict:
    """Convert generate_context_fingerprint() result to camelCase dict."""
    preset = ctx["preset"]
    config = ctx["config"]
    nav = preset.get("navigator", {})
    screen = preset.get("screen", {})
    webgl = preset.get("webgl", {})

    return {
        "initScript": _ensure_speech_voices_init_script(ctx["init_script"]),
        "contextOptions": {
            "userAgent": ctx["context_options"].get("user_agent"),
            "viewport": ctx["context_options"].get("viewport"),
            "deviceScaleFactor": ctx["context_options"].get("device_scale_factor"),
            "locale": ctx["context_options"].get("locale"),
            "timezoneId": ctx["context_options"].get("timezone_id"),
        },
        "camouConfig": config,
        "profileConfig": {
            "fontSpacingSeed": config.get("fonts:spacing_seed", 0),
            "audioSeed": config.get("audio:seed", 0),
            "canvasSeed": config.get("canvas:seed", 0),
            "screenWidth": screen.get("width", 1920),
            "screenHeight": screen.get("height", 1080),
            "screenColorDepth": screen.get("colorDepth", 24),
            "navigatorPlatform": nav.get("platform", ""),
            "navigatorOscpu": config.get("navigator.oscpu", ""),
            "navigatorUserAgent": config.get("navigator.userAgent", ""),
            "hardwareConcurrency": nav.get("hardwareConcurrency", 0),
            "webglVendor": webgl.get("unmaskedVendor", ""),
            "webglRenderer": webgl.get("unmaskedRenderer", ""),
            "timezone": config.get("timezone", preset.get("timezone", "")),
            "fontList": config.get("fonts", preset.get("fonts", [])),
            "speechVoices": _voice_names(config.get("voices", preset.get("speechVoices", []))),
        },
    }


def _screen_key(preset: dict) -> tuple[int, int]:
    pc = preset["profileConfig"]
    return (pc.get("screenWidth", 0), pc.get("screenHeight", 0))


def _is_software_webgl_preset(preset: dict) -> bool:
    pc = preset.get("profileConfig", {})
    webgl_text = " ".join(
        str(value).lower()
        for value in (pc.get("webglVendor", ""), pc.get("webglRenderer", ""))
    )
    return any(
        marker in webgl_text
        for marker in (
            "llvmpipe",
            "swiftshader",
            "software rasterizer",
            "softpipe",
            "lavapipe",
        )
    )


def _generate_hardware_webgl_preset(generate_context_fingerprint, os_name: str) -> dict:
    last_preset = None
    max_attempts = 100
    for _ in range(max_attempts):
        preset = convert_preset(generate_context_fingerprint(os=os_name))
        if not _is_software_webgl_preset(preset):
            return preset
        last_preset = preset
    raise RuntimeError(
        f"Unable to generate a non-software WebGL preset for {os_name} "
        f"after {max_attempts} attempts; last renderer was "
        f"{last_preset['profileConfig'].get('webglRenderer', '') if last_preset else 'unknown'}"
    )


def _generate_unique_screen_presets(
    generate_context_fingerprint, os_name: str, count: int
) -> list[dict]:
    presets = []
    seen_screens = set()
    attempts = 0
    max_attempts = max(count * 20, count)

    while len(presets) < count and attempts < max_attempts:
        attempts += 1
        preset = _generate_hardware_webgl_preset(generate_context_fingerprint, os_name)
        screen_key = _screen_key(preset)
        if screen_key in seen_screens:
            continue
        seen_screens.add(screen_key)
        presets.append(preset)

    while len(presets) < count:
        presets.append(_generate_hardware_webgl_preset(generate_context_fingerprint, os_name))

    return presets


def generate_presets() -> dict:
    generate_context_fingerprint = _load_generate_context_fingerprint()

    print("  Generating 3 macOS per-context profiles...")
    mac_per_context = _generate_unique_screen_presets(
        generate_context_fingerprint, "macos", 3
    )
    print("  Generating 3 Linux per-context profiles...")
    linux_per_context = _generate_unique_screen_presets(
        generate_context_fingerprint, "linux", 3
    )
    print("  Generating macOS global profile...")
    mac_global = _generate_hardware_webgl_preset(generate_context_fingerprint, "macos")
    print("  Generating Linux global profile...")
    linux_global = _generate_hardware_webgl_preset(generate_context_fingerprint, "linux")

    return {
        "macPerContext": mac_per_context,
        "linuxPerContext": linux_per_context,
        "macGlobal": mac_global,
        "linuxGlobal": linux_global,
    }


# ─── Preset Injection ─────────────────────────────────────────────────────────

def inject_timezone(preset: dict, timezone: str) -> None:
    preset["initScript"] = re.sub(
        r"w\.setTimezone\(Intl\.DateTimeFormat\(\)\.resolvedOptions\(\)\.timeZone\)",
        f"w.setTimezone({json.dumps(timezone)})",
        preset["initScript"],
    )
    preset["contextOptions"]["timezoneId"] = timezone
    preset["profileConfig"]["timezone"] = timezone
    preset["camouConfig"]["timezone"] = timezone


def inject_webrtc_ip(preset: dict) -> None:
    preset["initScript"] = re.sub(
        r'w\.setWebRTCIPv4\(""\)',
        f"w.setWebRTCIPv4({json.dumps(WEBRTC_TEST_IP)})",
        preset["initScript"],
    )


# ─── Profile Config ───────────────────────────────────────────────────────────

def preset_to_profile_config(preset: dict, name: str, os_type: str, mode: str) -> dict:
    pc = preset["profileConfig"]
    return {
        "name": name,
        "os": os_type,
        "mode": mode,
        "platform": pc["navigatorPlatform"],
        "oscpu": pc["navigatorOscpu"],
        "userAgent": pc["navigatorUserAgent"],
        "hardwareConcurrency": pc["hardwareConcurrency"],
        "screenWidth": pc["screenWidth"],
        "screenHeight": pc["screenHeight"],
        "colorDepth": pc["screenColorDepth"],
        "timezone": pc["timezone"],
        "webglVendor": pc["webglVendor"],
        "webglRenderer": pc["webglRenderer"],
    }
