# Packs the Android app (no Gradle): the manifest linked by aapt2, lib/arm64-v8a/ (the loader libnxloader.so and a host per game, libdmhost_<title ID>.so), classes.dex
# from dm/android/java (javac, then d8), the self test in assets/selftest (the synthetic module of dm/android/tests/selftest_nso.py, rewritten with DM_TARGET=android),
# page-aligned by zipalign, signed by apksigner.
# Usage: build_apk.py --sdk <Android SDK> --loader <libnxloader.so> --so <libdmhost_<id>.so> [--so ...] --selftest <folder of main.patched.bin, main.fixups.tsv, ...>
#   [--demo <folder>] --keystore <.jks> -o <out.apk> [--label <app name>] [--icon <icon.jpg or .png>] [--build-tools 36.0.0] [--platform android-35]
# The app is "nxnative" with the adaptive icon in apk/res: its launcher shows the games' own icons and names. --label and --icon name it otherwise.
# A game app instead: build_apk.py --sdk <SDK> --game-app <title ID> --so libdmhost_<title ID>.so --game-dir <folder> [--romfs <RomFS.img>] --keystore <.jks> -o <out.apk>
#   --label <name> --icon <icon.jpg>: one game in an app of its own (apk/AndroidManifest.game.xml, package game_package(<title ID>)), entered through GameStart with no
#   launcher or loader. --game-dir is the game's folder as the launcher's games/<title ID>/ holds it, packed into assets/game/; --romfs packs the RomFS image there as a
#   stored entry, which GameStart hands to the host where it lies in the APK. The APK stays a plain ZIP (no ZIP64, which apksigner can't read): Python's zipfile
#   keeps it under 2 GiB, so a bigger game ships its RomFS beside the APK (build_android_from_nsp.py --game-apk).
# The keystore is made on first use (keytool, a debug key: alias nxnative, password "android"); keep it outside the repository.
import argparse, os, shutil, subprocess, sys, tempfile, zipfile
from xml.sax.saxutils import quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))


def game_package(title_id):   # a game app's package: a name segment may not start with a digit, as a title ID does
    return 'org.nxnative.game.t' + title_id.lower()

def run(*a):
    r = subprocess.run(a, capture_output=True, text=True, errors='replace')
    if r.returncode: sys.exit(f'{a[0]} failed ({r.returncode}):\n{r.stdout}{r.stderr}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sdk', required=True); ap.add_argument('--selftest', help='the self test module (the nxnative app; not a game app)')
    ap.add_argument('--so', action='append', required=True, help='a game host, libdmhost_<title ID>.so (once per game)'); ap.add_argument('--loader', help='libnxloader.so (the nxnative app; not a game app)')
    ap.add_argument('--game-app', metavar='TITLE_ID', help='a game app: that game alone, entered through GameStart (its host is the one --so)')
    ap.add_argument('--game-dir', help="a game app's files, the launcher's games/<title ID>/ layout, for assets/game/"); ap.add_argument('--romfs', help="a game app's RomFS image, stored in assets/game/")
    ap.add_argument('--demo', help='folder for assets/demo: the synthetic shaders and their SPIR-V (tests/synth_shaders.py, tools/spv_android.py)')
    ap.add_argument('--keystore', required=True); ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--label', help="the app's name in the launcher (default nxnative)"); ap.add_argument('--icon', help="the app's launcher icon: a JPEG or PNG (the game's icon.jpg)")
    ap.add_argument('--build-tools', default='36.0.0'); ap.add_argument('--platform', default='android-35')
    a = ap.parse_args()
    for so in a.so:
        n = os.path.basename(so)
        if not (n.startswith('libdmhost_') and n.endswith('.so')): sys.exit(f'--so must be a game host named libdmhost_<title ID>.so: {so}')
    if a.game_app:
        if len(set(a.so)) != 1 or os.path.basename(a.so[0]) != f'libdmhost_{a.game_app.upper()}.so': sys.exit(f'--game-app {a.game_app} takes one --so, libdmhost_{a.game_app.upper()}.so')
        if not a.game_dir or not os.path.isfile(os.path.join(a.game_dir, 'game.json')): sys.exit('--game-app needs --game-dir with the game\'s files (game.json among them)')
    elif not (a.loader and a.selftest): sys.exit('the nxnative app needs --loader and --selftest')
    bt = os.path.join(a.sdk, 'build-tools', a.build_tools); exe = '.exe' if os.name == 'nt' else ''; bat = '.bat' if os.name == 'nt' else ''
    jar = os.path.join(a.sdk, 'platforms', a.platform, 'android.jar')
    if not os.path.exists(a.keystore):
        run('keytool', '-genkeypair', '-keystore', a.keystore, '-storepass', 'android', '-keypass', 'android', '-alias', 'nxnative',
            '-keyalg', 'RSA', '-keysize', '2048', '-validity', '10000', '-dname', 'CN=nxnative debug')
    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, 'base.apk'); aapt2 = os.path.join(bt, 'aapt2' + exe)
        # the manifest with the app's label and icon: the application element's label replaced, an icon attribute added beside it (a drawable compiled by aapt2)
        with open(os.path.join(HERE, '..', 'apk', 'AndroidManifest.game.xml' if a.game_app else 'AndroidManifest.xml'), encoding='utf-8') as f: manifest = f.read()
        if a.game_app: manifest = manifest.replace('@PACKAGE@', game_package(a.game_app)).replace('@LIB@', f'dmhost_{a.game_app.upper()}')
        # the label always: the application element's is replaced whole, and an app with none shows its package name (org.nxnative.android)
        attrs, res = f' android:label={quoteattr(a.label or "nxnative")}', []
        if a.icon:
            ext = os.path.splitext(a.icon)[1].lower()
            if ext not in ('.jpg', '.jpeg', '.png'): sys.exit(f'--icon must be a JPEG or PNG: {a.icon}')
            rd = os.path.join(d, 'res', 'drawable'); os.makedirs(rd); shutil.copyfile(a.icon, os.path.join(rd, 'icon' + ('.jpg' if ext == '.jpeg' else ext)))
            run(aapt2, 'compile', '--dir', os.path.join(d, 'res'), '-o', os.path.join(d, 'res.zip')); res = ['-R', os.path.join(d, 'res.zip'), '--auto-add-overlay']
            attrs += ' android:icon="@drawable/icon" android:roundIcon="@drawable/icon"'
        else:   # the app's own adaptive icon (apk/res: the mark on ink, vector drawables, with a monochrome layer for themed icons)
            run(aapt2, 'compile', '--dir', os.path.join(HERE, '..', 'apk', 'res'), '-o', os.path.join(d, 'res.zip')); res = ['-R', os.path.join(d, 'res.zip'), '--auto-add-overlay']
            attrs += ' android:icon="@mipmap/ic_launcher" android:roundIcon="@mipmap/ic_launcher"'
        if attrs:
            if '<application android:label="nxnative"' not in manifest: sys.exit('AndroidManifest.xml: no <application android:label="nxnative"> to name the app in')
            manifest = manifest.replace('<application android:label="nxnative"', '<application' + attrs, 1)
        mf = os.path.join(d, 'AndroidManifest.xml')
        with open(mf, 'w', encoding='utf-8') as f: f.write(manifest)
        run(aapt2, 'link', '-o', base, '--manifest', mf, '-I', jar, *res, '--min-sdk-version', '29', '--target-sdk-version', '34')
        # the Java side (the touch controls, the text box): javac for Java 8 against the platform's android.jar, then d8 to classes.dex
        src = os.path.join(HERE, '..', 'java'); cls = os.path.join(d, 'classes'); os.makedirs(cls)
        javas = [os.path.join(r, f) for r, _, fs in os.walk(src) for f in fs if f.endswith('.java')]
        run('javac', '-source', '8', '-target', '8', '-Xlint:-options', '-encoding', 'UTF-8', '-bootclasspath', jar, '-d', cls, *javas)
        run(os.path.join(bt, 'd8' + bat), '--lib', jar, '--min-api', '29', '--output', d, *[os.path.join(r, f) for r, _, fs in os.walk(cls) for f in fs if f.endswith('.class')])
        # a game app's ZIP without ZIP64 (allowZip64=False: past 2 GiB zipfile raises instead of writing records apksigner can't read)
        try:
            with zipfile.ZipFile(base, 'a', zipfile.ZIP_DEFLATED, allowZip64=not a.game_app) as z:
                z.write(os.path.join(d, 'classes.dex'), 'classes.dex')
                if a.loader: z.write(a.loader, 'lib/arm64-v8a/libnxloader.so')
                for so in sorted(set(a.so)): z.write(so, f'lib/arm64-v8a/{os.path.basename(so)}')
                if a.selftest:
                    n = 0
                    for f in sorted(os.listdir(a.selftest)):
                        if f.startswith('main.'): z.write(os.path.join(a.selftest, f), f'assets/selftest/{f}'); n += 1
                    if not n: sys.exit(f'no self test module (main.*) in {a.selftest}')
                for f in sorted(os.listdir(a.demo)) if a.demo else []: z.write(os.path.join(a.demo, f), f'assets/demo/{f}')
                if a.game_app:
                    # the game's files as games/<title ID>/ holds them (folders given as links are followed), then the RomFS image stored: GameStart opens it with
                    # AssetManager.openFd, which takes only an uncompressed entry, and the host reads it at its offset in the APK
                    for root, _, fs in sorted(os.walk(a.game_dir, followlinks=True)):
                        for f in sorted(fs):
                            path = os.path.join(root, f); rel = os.path.relpath(path, a.game_dir).replace(os.sep, '/')
                            if rel != 'RomFS.img': z.write(path, f'assets/game/{rel}')
                    if a.romfs: z.write(a.romfs, 'assets/game/RomFS.img', compress_type=zipfile.ZIP_STORED)
        except zipfile.LargeZipFile:
            sys.exit('the game app would be over 2 GiB, more than an APK without ZIP64 holds: pack the RomFS beside it (no --romfs)')
        aligned = os.path.join(d, 'aligned.apk')
        run(os.path.join(bt, 'zipalign' + exe), '-f', '-P', '16', '4', base, aligned)   # stored entries (a game app's RomFS) on 4 bytes, the libraries on 16 KiB pages
        run(os.path.join(bt, 'apksigner' + bat), 'sign', '--ks', a.keystore, '--ks-pass', 'pass:android', '--key-pass', 'pass:android', '--out', a.out, aligned)
    print(f'{a.out}: {os.path.getsize(a.out):,} bytes')

if __name__ == '__main__':
    main()
