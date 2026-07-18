# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_data_files

_datas = [('assets', 'assets')]

# flet本体が同梱するicons.json/cupertino_icons.json等の非Pythonデータファイルは
# PyInstallerの自動解析では検出されないため、collect_data_filesで明示的に回収する
_datas += collect_data_files('flet')

# flet_desktopのデスクトップクライアント本体(flet-macos.tar.gz等)は実行時にネットワーク
# からダウンロードされる仕様のため、事前に `<flet_desktop>/app/` 配下に配置しておくと
# ダウンロードせずそのまま同梱できる(flet_desktop.ensure_client_cached()の挙動による)。
# 事前配置していない場合はビルド自体は成功するが、初回起動時にネットワークダウンロードが発生する。
try:
    import flet_desktop
    _flet_desktop_bin_dir = os.path.join(os.path.dirname(flet_desktop.__file__), 'app')
    if os.path.isdir(_flet_desktop_bin_dir) and os.listdir(_flet_desktop_bin_dir):
        _datas.append((_flet_desktop_bin_dir, 'flet_desktop/app'))
except ImportError:
    pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    # flet_desktopは実行時に動的解決されるためPyInstallerの静的解析だけでは検出されず、
    # 明示的にhiddenimportsへ追加しないと凍結後に ModuleNotFoundError になる
    hiddenimports=['flet_desktop'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# OSごとにアイコンパスを切り替える(Windows: .ico / macOS: .icns)
_icon = 'assets/icon.ico' if sys.platform == 'win32' else 'assets/icon.icns'

# onedirモードでビルドする(PyInstaller: onefile+macOS BUNDLEの組み合わせは非推奨でv7以降エラーになるため)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PixivVault',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    # macOSでは pixivvault:// 起動をApple EventからsysargvへブリッジするためTrueにする
    argv_emulation=(sys.platform == 'darwin'),
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PixivVault',
)

# macOSの場合のみ .app アプリケーションバンドルを生成する
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='PixivVault.app',
        icon='assets/icon.icns',
        bundle_identifier='com.pixivvault.app',
        info_plist={
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
            # 実UIは~/.flet/client配下のFlet.app(main.pyでPixivVaultにブランディング)が担うため、
            # 本体側はDock/Cmd+Tabに表示させずDock二重表示を防ぐ
            'LSUIElement': True,
            'CFBundleURLTypes': [
                {
                    'CFBundleURLName': 'PixivVault Protocol',
                    'CFBundleURLSchemes': ['pixivvault'],
                }
            ],
        },
    )
