#!/usr/bin/env python3

"""
The script that patches the Firefox source into the Camoufox source.
Based on LibreWolf's patch script:
https://gitlab.com/librewolf-community/browser/source/-/blob/main/scripts/librewolf-patches.py

Run:
    python3 scripts/init-patch.py <version> <release>
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from _mixin import (
    find_src_dir,
    get_moz_target,
    get_options,
    list_patches,
    patch,
    run,
    temp_cd,
)

options, args = get_options()

"""
Main patcher functions
"""


@dataclass
class Patcher:
    """Patch and prepare the Camoufox source"""

    moz_target: str
    target: str

    def camoufox_patches(self):
        """
        Apply all patches
        """
        version, release = extract_args()
        with temp_cd(find_src_dir('.', version, release)):
            run('git config core.autocrlf false', exit_on_fail=False)
            run('git config core.eol lf', exit_on_fail=False)

            if options.mozconfig_only:
                base_mozconfig = os.path.join("..", "assets", "base.mozconfig")
                if not os.path.exists("mozconfig") and os.path.exists(base_mozconfig):
                    shutil.copy2(base_mozconfig, "mozconfig")
                print("Updating mozconfig only...")
                print(f'Using target: {self.moz_target}')
                self._update_mozconfig()
                print('Complete!')
                return

            # Reset to unpatched state first (like "Find broken patches")
            print("Resetting to unpatched state...")
            run('git clean -fdx', exit_on_fail=False)
            run('mach.cmd clobber' if os.name == 'nt' else './mach clobber', exit_on_fail=False)
            run('git reset --hard unpatched', exit_on_fail=False)

            # Re-copy additions and settings after reset
            print("Re-copying additions and settings...")
            run(f'bash ../scripts/copy-additions.sh {version} {release}')

            # Create the base mozconfig file
            run('cp -v ../assets/base.mozconfig mozconfig')
            # Set cross building target
            print(f'Using target: {self.moz_target}')
            self._update_mozconfig()

            if not options.mozconfig_only:
                # Apply patches with roverfox patches at the very end
                all_patches = list_patches()
                # Normalize paths and partition into non-roverfox and roverfox
                non_roverfox = []
                roverfox = []
                for p in all_patches:
                    norm = os.path.normpath(p)
                    parts = norm.split(os.sep)
                    if 'roverfox' in parts:
                        roverfox.append(p)
                    else:
                        non_roverfox.append(p)

                # Track patch failures
                failed_patches = []

                # Apply non-roverfox patches first
                for patch_file in non_roverfox:
                    rejects = self._apply_and_check(patch_file)
                    if rejects:
                        failed_patches.append((patch_file, rejects))

                # Apply roverfox patches last
                for patch_file in roverfox:
                    rejects = self._apply_and_check(patch_file)
                    if rejects:
                        failed_patches.append((patch_file, rejects))

                # Report failures
                if failed_patches:
                    print('\n' + '='*70)
                    print(f'ERROR: {len(failed_patches)} patch(es) failed to apply cleanly:')
                    print('='*70)
                    for patch_file, rejects in failed_patches:
                        print(f'\n{patch_file}:')
                        for reject in rejects:
                            print(f'  - {reject}')
                    print('='*70)
                    sys.exit(1)

            print('Complete!')

    def _apply_and_check(self, patch_file):
        """
        Apply a patch and check for reject files.
        Returns list of reject files if any, empty list otherwise.
        """
        import time

        print(f"\n*** -> patch -p1 -i {patch_file}")
        sys.stdout.flush()

        # Record time before applying so we only detect .rej files from this patch
        start_time = time.time()

        # Apply patch interactively - don't capture stdout/stderr at all
        # This allows prompts to show immediately and user can respond
        # --forward flag: skip patches that appear to be already applied
        # On Windows, `patch --binary` rejects LF hunks against CRLF working-tree
        # files with "different line endings". Text mode lets GNU patch
        # normalize line endings during application.
        # -l flag: ignore whitespace differences
        patch_cmd = ['patch', '-p1', '--forward', '-l', '-i', patch_file]
        if os.name != 'nt':
            patch_cmd.insert(4, '--binary')

        result = subprocess.run(
            patch_cmd,
            stdin=sys.stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        sys.stdout.flush()
        sys.stderr.flush()

        # After patch completes, search for any .rej files created during this patch
        rejects = []
        for root, dirs, files in os.walk('.'):
            for file in files:
                if file.endswith('.rej'):
                    reject_path = os.path.join(root, file)
                    if os.path.exists(reject_path):
                        # Only include if created after this patch started
                        if os.path.getmtime(reject_path) >= start_time:
                            rejects.append(reject_path)

        # Clean up .rej files so they don't interfere with subsequent patches
        for rej in rejects:
            try:
                if os.path.exists(rej):
                    os.remove(rej)
            except FileNotFoundError:
                # Another patch step may have already removed the reject file.
                continue

        output = f"{result.stdout}\n{result.stderr}"
        has_real_failure = any(
            marker in output
            for marker in (
                'FAILED at',
                'malformed patch',
                'Only garbage was found',
                'No file to patch',
                "can't find file to patch",
                'patch unexpectedly ends in middle of line',
            )
        )

        # `patch --forward` can emit `.rej` files when hunks are already applied or
        # when file-creation hunks are skipped because the file already exists.
        # Those are benign for our layered patch stack and should not fail the build.
        if rejects and not has_real_failure:
            rejects = []

        if result.returncode != 0 and not rejects and has_real_failure:
            rejects.append(f'patch exited with status {result.returncode}')

        return rejects

    def _update_mozconfig(self):
        """
        Helper for adding additional mozconfig code from assets/<target>.mozconfig
        """
        base_mozconfig = os.path.join("..", "assets", "base.mozconfig")
        mozconfig_backup = "mozconfig.backup"
        mozconfig = "mozconfig"
        mozconfig_hash = "mozconfig.hash"

        # Create backup if it doesn't exist
        if not os.path.exists(mozconfig_backup):
            if os.path.exists(base_mozconfig):
                shutil.copy2(base_mozconfig, mozconfig_backup)
            elif os.path.exists(mozconfig):
                shutil.copy2(mozconfig, mozconfig_backup)
            else:
                with open(mozconfig_backup, 'w', encoding='utf-8') as f:
                    pass

        # Read backup content
        with open(mozconfig_backup, 'r', encoding='utf-8') as f:
            content = f.read()

        # Add target option
        content += f"\nac_add_options --target={self.moz_target}\n"

        # Add target-specific mozconfig if it exists
        target_mozconfig = os.path.join("..", "assets", f"{self.target}.mozconfig")
        if os.path.exists(target_mozconfig):
            with open(target_mozconfig, 'r', encoding='utf-8') as f:
                content += f.read()

        # Calculate new hash
        new_hash = hashlib.sha256(content.encode()).hexdigest()

        # Update mozconfig
        print(f"-> Updating mozconfig, target is {self.moz_target}")
        with open(mozconfig, 'w', encoding='utf-8') as f:
            f.write(content)
        with open(mozconfig_hash, 'w', encoding='utf-8') as f:
            f.write(new_hash)


def add_rustup(*targets):
    """Add rust targets"""
    rustup = shutil.which("rustup")
    if not rustup:
        cargo_bin = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
        candidates = [os.path.join(cargo_bin, "rustup")]
        if os.name == "nt":
            candidates.insert(0, os.path.join(cargo_bin, "rustup.exe"))
        rustup = next((path for path in candidates if os.path.isfile(path)), None)

    if not rustup:
        sys.stderr.write(
            "error: rustup executable not found. Install Rust via rustup and ensure it is on PATH.\n"
        )
        sys.exit(1)

    for rust_target in targets:
        cmd = [rustup, "target", "add", rust_target]
        print(" ".join(f'"{part}"' if " " in part else part for part in cmd))
        sys.stdout.flush()
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"fatal error: command '{cmd}' failed")
            sys.stdout.flush()
            sys.exit(result.returncode)


def _update_rustup(target):
    """Add rust targets for the given target"""
    if target == "linux":
        add_rustup("aarch64-unknown-linux-gnu", "i686-unknown-linux-gnu")
    elif target == "windows":
        add_rustup("x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc", "i686-pc-windows-msvc")
    elif target == "macos":
        add_rustup("x86_64-apple-darwin", "aarch64-apple-darwin")


"""
Preparation
"""


def extract_args():
    """Get version and release from args"""
    if len(args) != 2:
        sys.stderr.write('error: please specify version and release of camoufox source')
        sys.exit(1)
    return args[0], args[1]


AVAILABLE_TARGETS = ["linux", "windows", "macos"]
AVAILABLE_ARCHS = ["x86_64", "arm64", "i686"]


def extract_build_target():
    """Get moz_target if passed to BUILD_TARGET environment variable"""

    if os.environ.get('BUILD_TARGET'):
        parts = [part.strip() for part in os.environ['BUILD_TARGET'].split(',', 1)]
        assert len(parts) == 2, (
            f"BUILD_TARGET must be '<target>,<arch>', got: {os.environ['BUILD_TARGET']}"
        )
        target, arch = parts
        assert target in AVAILABLE_TARGETS, f"Unsupported target: {target}"
        assert arch in AVAILABLE_ARCHS, f"Unsupported architecture: {arch}"
    else:
        target, arch = "macos", "arm64"
    return target, arch


"""
Launcher
"""

if __name__ == "__main__":
    # Extract args
    VERSION, RELEASE = extract_args()

    TARGET, ARCH = extract_build_target()
    MOZ_TARGET = get_moz_target(TARGET, ARCH)
    _update_rustup(TARGET)

    # Check if the folder exists
    if not os.path.exists(f'camoufox-{VERSION}-{RELEASE}/configure.py'):
        sys.stderr.write('error: folder doesn\'t look like a Firefox folder.')
        sys.exit(1)

    # Apply the patches
    patcher = Patcher(MOZ_TARGET, TARGET)
    patcher.camoufox_patches()

    sys.exit(0)  # ensure 0 exit code
