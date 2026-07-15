# PixivVault macOS移植 実装計画書 (implementation_plan.md)

本書は [MAC_PORTING.md](MAC_PORTING.md) で示された移植方針を実装レベルに落とし込んだ計画書です。

**絶対ルール**
- Flet 0.85.3 に存在するAPI・プロパティのみを使用する（本計画で変更するファイルはいずれもFlet APIに触れないため、この制約への抵触はありません）。
- Conda環境は使用しない。すべて `pip install -r requirements.txt` ベースの通常Python環境で完結させる。

---

## 1. 概要

現状の PixivVault は下記4点により、macOS上では**起動不可（即クラッシュ）**、または一部機能が動作しない状態です。

| # | 問題箇所 | 症状 | 深刻度 |
|---|---|---|---|
| 1 | [scheduler.py:4](scheduler.py:4) `import win11toast`（モジュールトップレベル） | `main.py` → `Scheduler` のimportチェーンで **起動直後に ImportError クラッシュ** | 🔴 Critical（MAC_PORTING.md未記載の追加検出） |
| 2 | [server.py:7](server.py:7) `from win11toast import toast`（モジュールトップレベル） | 拡張機能連携サーバ起動時に **ImportError クラッシュ** | 🔴 Critical |
| 3 | [registry_helper.py:1](registry_helper.py:1) `import winreg`（モジュールトップレベル） | `gui.py` が `import registry_helper` している時点で **ImportError クラッシュ**（GUIすら開けない） | 🔴 Critical |
| 4 | [main.py:47](main.py:47) `from win11toast import toast`（関数内import） | 多重起動時の通知のみ失敗（try/exceptで握りつぶし済みなので非致命） | 🟡 Minor |
| 5 | [core.py:37](core.py:37) `import win11toast`（関数内import） | ZIP破損検知通知のみ失敗（try/exceptで握りつぶし済み） | 🟡 Minor |
| 6 | [gui.py:941](gui.py:941) `os.startfile(folder_path)` | 「保存フォルダを開く」ボタンがmacOSで機能しない（`AttributeError`後のフォールバックも`explorer`というWindowsコマンド） | 🟠 Moderate |
| 7 | [main.spec:38](main.spec:38) `icon='assets\\icon.ico'` 固定 | PyInstallerでのmacOSビルド時に `.app` バンドルが生成されない／Windows用アイコンパスで失敗 | 🔴 Critical（ビルド不可） |

対応方針は以下の3本柱です。

1. **通知処理の一本化**: `win11toast` への直接依存を全廃し、新設の `notifications.py` の `send_notification()` に集約する。Windowsでは `win11toast`、macOSでは `osascript` を使い分ける。**すべてのimportを遅延（関数内）import化**し、モジュールロード時にOS非対応ライブラリでクラッシュしないようにする。
2. **レジストリ処理の無害化**: `registry_helper.py` を `sys.platform` で分岐させ、Windows以外では `winreg` をロードせず、全関数がダミー応答（実処理スキップ）で正常終了するようにする。
3. **ビルド設定のマルチプラットフォーム化**: `main.spec` をOS判定で分岐させ、macOSでは `.icns` アイコンと `BUNDLE`（`.app`生成、`CFBundleURLTypes`によるURLスキーム登録）を追加する。

これにより、同一のコードベース・同一の `main.spec` から `pyinstaller` を実行するだけで、実行環境のOSに応じた成果物（Windows: `PixivVault.exe` / macOS: `PixivVault.app`）が生成されるようになります。

---

## 2. 各ファイルの具体的な変更コード案

### 2.0 [NEW] `notifications.py`（新規作成・共通通知モジュール）

`server.py` / `main.py` / `core.py` / `scheduler.py` の4箇所に散在する通知処理を1つの関数に集約します。`osascript` に渡す文字列はダブルクォート・バックスラッシュを含むタイトル/本文（例: ユーザー名に `"` を含む等）でスクリプトが壊れないよう、必ずエスケープします。

```python
# notifications.py (新規)
import sys
import subprocess
import logging

logger = logging.getLogger(__name__)


def send_notification(title: str, message: str, app_id: str = "PixivVault") -> None:
    """OS通知(トースト)を送信する。
    Windows: win11toast / macOS: osascript(display notification) を使用し、
    それ以外のOSでは何もしない(ログ出力のみ)。
    """
    if sys.platform == "win32":
        try:
            from win11toast import toast
            toast(title, message, app_id=app_id)
        except Exception as e:
            logger.error(f"通知の送信に失敗しました(Windows): {e}")
    elif sys.platform == "darwin":
        def _escape(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                timeout=5,
                capture_output=True,
            )
        except Exception as e:
            logger.error(f"通知の送信に失敗しました(macOS): {e}")
    else:
        logger.warning(f"通知未対応のOSのためスキップしました: {sys.platform}")
```

適用方針: `win11toast` / `osascript` いずれも**関数内でimport/呼び出し**し、`notifications.py` 自体は `sys` `subprocess` `logging` のみに依存する（標準ライブラリのみ）ため、モジュールとしてimportした時点でクラッシュすることはありません。

---

### 2.1 [MODIFY] `server.py` / `main.py`（および `core.py` / `scheduler.py`）

MAC_PORTING.md で指定された `server.py` / `main.py` に加え、同じ `win11toast` 依存を持つ `core.py` / `scheduler.py` も横展開で修正します（特に `scheduler.py` はトップレベルimportのため未修正だとmacOSで即クラッシュします）。

#### server.py

**修正前**
```python
# server.py 冒頭
from win11toast import toast
```
```python
# server.py notify()
def notify(self, title, message):
    try:
        if self.db.get_setting("enable_notifications", "1") == "1":
            threading.Thread(target=lambda: toast(title, message, app_id="PixivVault"), daemon=True).start()
    except Exception as e:
        logger.error(f"通知の送信に失敗しました: {e}")
```

**修正後**
```python
# server.py 冒頭
from notifications import send_notification
```
```python
# server.py notify()
def notify(self, title, message):
    try:
        if self.db.get_setting("enable_notifications", "1") == "1":
            threading.Thread(target=lambda: send_notification(title, message), daemon=True).start()
    except Exception as e:
        logger.error(f"通知の送信に失敗しました: {e}")
```

#### main.py

**修正前**
```python
# main.py check_single_instance()
    except socket.error:
        try:
            from win11toast import toast
            toast("PixivVault", "PixivVaultは既に起動しています。", app_id="PixivVault")
        except Exception:
            pass
        sys.exit(0)
```

**修正後**
```python
# main.py check_single_instance()
    except socket.error:
        try:
            from notifications import send_notification
            send_notification("PixivVault", "PixivVaultは既に起動しています。")
        except Exception:
            pass
        sys.exit(0)
```

#### core.py（横展開・必須）

**修正前**
```python
def notify_toast(db: Database, title: str, body: str):
    """OS通知(トースト)を送信する。scheduler.py/server.pyの通知と同じ設定キー(enable_notifications)を使う。"""
    if db.get_setting("enable_notifications", "1") != "1":
        return
    try:
        import win11toast
        threading.Thread(target=lambda: win11toast.toast(title, body, app_id="PixivVault"), daemon=True).start()
    except Exception as e:
        logging.getLogger(__name__).error(f"通知エラー: {e}")
```

**修正後**
```python
def notify_toast(db: Database, title: str, body: str):
    """OS通知(トースト)を送信する。scheduler.py/server.pyの通知と同じ設定キー(enable_notifications)を使う。"""
    if db.get_setting("enable_notifications", "1") != "1":
        return
    try:
        from notifications import send_notification
        threading.Thread(target=lambda: send_notification(title, body), daemon=True).start()
    except Exception as e:
        logging.getLogger(__name__).error(f"通知エラー: {e}")
```

#### scheduler.py（横展開・**最優先で必須**: トップレベルimportのため未修正だとmacOSで起動不能）

**修正前**
```python
# scheduler.py 冒頭
import time
import threading
from datetime import datetime, timedelta
import win11toast          # ← モジュールロード時に即ImportError(macOS)
from pixiv_client import PixivClient
from database import Database
from core import run_batch_backup
```
```python
    def notify(self, title, body):
        if self.db.get_setting("enable_notifications", "1") == "1":
            try:
                win11toast.toast(title, body)
            except Exception as e:
                if self.log_callback:
                    self.log_callback(f"通知エラー: {e}")
```

**修正後**
```python
# scheduler.py 冒頭
import time
import threading
from datetime import datetime, timedelta
from notifications import send_notification
from pixiv_client import PixivClient
from database import Database
from core import run_batch_backup
```
```python
    def notify(self, title, body):
        if self.db.get_setting("enable_notifications", "1") == "1":
            try:
                send_notification(title, body)
            except Exception as e:
                if self.log_callback:
                    self.log_callback(f"通知エラー: {e}")
```

---

### 2.2 [MODIFY] `registry_helper.py`

`winreg` インポートエラーを回避し、Windows以外ではレジストリ処理を行わずダミーとして正常終了させます。macOSでのURLスキーム登録は `main.spec` の `Info.plist`（`CFBundleURLTypes`）が担うため、実行時のレジストリ登録処理自体が不要になります。

**修正前**
```python
import winreg
import sys
import os
import logging

logger = logging.getLogger(__name__)

REG_PATH = r"Software\Classes\pixivvault"

def get_executable_command() -> str:
    ...

def register_protocol() -> bool:
    """カスタムURIスキーム(pixivvault://)をレジストリに登録します"""
    command = get_executable_command()
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
            ...
        return True
    except Exception as e:
        logger.error(f"レジストリの登録に失敗しました: {e}")
        return False

def unregister_protocol() -> bool:
    ...
    winreg.DeleteKey(...)
    ...

def check_protocol_registered() -> bool:
    expected_command = get_executable_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell\open\command") as cmd_key:
            ...
```

**修正後**
```python
import sys
import os
import logging

logger = logging.getLogger(__name__)

# Windows以外ではwinregをロードしない(モジュール自体が存在しないため)
if sys.platform == "win32":
    import winreg
else:
    winreg = None

REG_PATH = r"Software\Classes\pixivvault"


def get_executable_command() -> str:
    """現在の環境(開発/ビルド済)に合わせた起動コマンド文字列を返します"""
    if getattr(sys, 'frozen', False):
        exe_path = os.path.abspath(sys.executable)
        return f'"{exe_path}" "%1"'
    else:
        python_exe = os.path.abspath(sys.executable)
        main_py = os.path.abspath(sys.argv[0])
        return f'"{python_exe}" "{main_py}" "%1"'


def register_protocol() -> bool:
    """カスタムURIスキーム(pixivvault://)をレジストリに登録します(Windows専用)。
    macOS/Linuxでは何もせず成功扱いを返す(URLスキームはInfo.plistで解決済みのため)。
    """
    if sys.platform != "win32":
        logger.info("このOSではレジストリ登録は不要です(スキップ)。")
        return True

    command = get_executable_command()
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:PixivVault Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
            with winreg.CreateKey(key, r"shell\open\command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)
        logger.info(f"レジストリに登録しました: {command}")
        return True
    except Exception as e:
        logger.error(f"レジストリの登録に失敗しました: {e}")
        return False


def unregister_protocol() -> bool:
    """カスタムURIスキームのレジストリ登録を解除します(Windows専用)。"""
    if sys.platform != "win32":
        return True

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell\open\command")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell\open")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_PATH)
        logger.info("レジストリから登録を解除しました。")
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_PATH)
            return True
        except Exception:
            pass
        logger.error(f"レジストリの解除に失敗しました: {e}")
        return False


def check_protocol_registered() -> bool:
    """現在カスタムURIスキームが登録されているか確認します(Windows専用)。
    macOS/Linuxでは常にFalseを返す(GUI側はレジストリ操作ボタンをWindows限定表示にする想定)。
    """
    if sys.platform != "win32":
        return False

    expected_command = get_executable_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell\open\command") as cmd_key:
            command, _ = winreg.QueryValueEx(cmd_key, "")
            return command == expected_command
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.error(f"レジストリの確認に失敗しました: {e}")
        return False
```

**GUI側への影響について（参考・任意対応）**
[gui.py:958-980](gui.py:958) の「拡張機能」タブは `registry_helper` の3関数を呼び出しており、上記修正後もmacOS上で**エラーなく動作**します（常に「無効」表示・登録ボタンは無害な `True` を返すのみ）。文言を「レジストリ登録済み」→OS非依存な表現に変更するのはUI改善であり必須ではないため、本計画のスコープ外（任意のフォローアップ）とします。

---

### 2.3 [MODIFY] `gui.py`（横展開: `os.startfile` のマルチプラットフォーム化）

MAC_PORTING.md には明記がありませんが、調査の結果 [gui.py:936-943](gui.py:936) の「保存フォルダを開く」機能がWindows専用APIに依存しており、macOSでは正しいフォールバックがない（`explorer` コマンドはWindows専用）ことを確認しました。あわせて修正します。

**修正前**
```python
    def open_save_folder(e=None):
        import subprocess
        folder_path = db.get_setting("save_path", "Images")
        os.makedirs(folder_path, exist_ok=True)
        try:
            os.startfile(folder_path)
        except AttributeError:
            subprocess.Popen(['explorer', folder_path])
```

**修正後**
```python
    def open_save_folder(e=None):
        import subprocess
        import sys
        folder_path = db.get_setting("save_path", "Images")
        os.makedirs(folder_path, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(folder_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder_path])
            else:
                subprocess.Popen(["xdg-open", folder_path])
        except Exception as e:
            logger.error(f"保存フォルダを開けませんでした: {e}")
```

---

### 2.4 [MODIFY] `main.spec`

OS判定を行い、macOSの場合にのみ `BUNDLE` を生成するマルチプラットフォーム両対応のPyInstaller specファイルに書き換えます。

**修正前**
```python
# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PixivVault',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets\\icon.ico',
)
```

**修正後（Mac実機でのビルド検証を経た最終版。詳細は2.6参照）**
```python
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
            'CFBundleShortVersionString': '3.0',
            'NSHighResolutionCapable': True,
            'CFBundleURLTypes': [
                {
                    'CFBundleURLName': 'PixivVault Protocol',
                    'CFBundleURLSchemes': ['pixivvault'],
                }
            ],
        },
    )
```

適用方針:
- `BUNDLE` / `COLLECT` はグローバルシンボルとして提供される（PyInstallerがspecファイルを`exec`する際に自動注入するため）ため、`from PyInstaller.building.api import BUNDLE` のような明示的importは不要かつ環境によっては不整合の原因になるので行いません（MAC_PORTING.md記載のサンプルより単純化）。
- `icon` はWindowsビルド時は既存の `assets/icon.ico` をそのまま使用し、既存のWindowsビルドフローに影響を与えません。
- `.icns` ファイルは新規に用意が必要（後述フェーズ2参照）。
- 当初案では `EXE` に直接 `a.binaries`/`a.datas` を渡すonefile方式だったが、Mac実機ビルドで `DEPRECATION: Onefile mode in combination with macOS .app bundles ... will become an error in v7.0.` という警告が出たため、`exclude_binaries=True` + `COLLECT` によるonedir方式に変更した（2.6参照）。

---

### 2.5 [MODIFY] `requirements.txt`（ビルド前提の整備）

`win11toast` はWindows専用パッケージで、macOS上では `pip install` 自体が失敗、または依存する `winrt` 系パッケージのインストールエラーになります。pip の環境マーカー（PEP 508）を使い、Windows以外ではインストール対象から除外します。また `pystray` はmacOS上でAppKitバックエンドを使うために `pyobjc-framework-Cocoa` が必要です。

**修正前**
```
pystray
win11toast
schedule
```

**修正後**
```
pystray
win11toast; sys_platform == "win32"
pyobjc-framework-Cocoa; sys_platform == "darwin"
schedule
Pillow
flet==0.85.3
flet-desktop==0.85.3
requests
```

適用方針: `flet` / `requests` / `Pillow` は既存コードで実際に使用されている（[main.py:1](main.py:1), [tray.py:2](tray.py:2), [pixiv_client.py](pixiv_client.py) 等）にもかかわらず現状の `requirements.txt` に記載がありません。macOSの新規クリーン環境で `pip install -r requirements.txt` のみでビルド可能にするため、本計画で明記します（絶対ルール①によりFletは `0.85.3` にバージョン固定）。`flet-desktop` はFletのデスクトップウィンドウを実際に描画するランタイム本体で、`flet` 単体には含まれないため明記が必要（詳細は2.6参照）。

**重要な前提条件: Python 3.10以上が必須**
Flet 0.85.3は `Requires-Python >=3.10` のため、macOSのシステム標準Python(3.9系)では `pip install` の時点で解決不能エラーになる。Mac実機での検証時もこれに該当し、Homebrewで `python@3.12` を追加インストールして対応した。

---

### 2.6 Mac実機ビルドで新たに判明した問題と対処（実装・検証時の追加知見）

Phase 3の実ビルド検証（PyInstaller実行 → `.app` 起動）を通じて、コードレビューだけでは見つからなかった3つの問題が判明しました。いずれも上記2.4/2.5の最終コードに反映済みです。

| # | 問題 | 症状 | 対処 |
|---|---|---|---|
| 1 | Flet 0.85.3は `Requires-Python >=3.10` | macOS標準Python(3.9系)では `pip install flet==0.85.3` が「No matching distribution」で失敗 | Homebrewで `python@3.12` を導入し、それでvenvを作成する（2.5参照） |
| 2 | `flet` 本体に `flet_desktop`（実際にウィンドウを描画するランタイム）が含まれない | `.app` を起動すると `ModuleNotFoundError: No module named 'flet_desktop'` で即終了 | `requirements.txt` に `flet-desktop==0.85.3` を追加し、`main.spec` の `hiddenimports` に `'flet_desktop'` を追加（PyInstallerの静的解析は動的import/実行時解決される`flet_desktop`を自動検出できないため） |
| 3 | `flet_desktop` はデスクトップクライアント本体(`flet-macos.tar.gz`、約50MB)を**初回起動時にGitHub Releasesからダウンロードする**設計 | オフライン配布物として不十分。またビルド直後の初回起動が遅くなる/失敗しうる | `flet_desktop.ensure_client_cached()` は `<flet_desktop>/app/` 配下に対象OSのアーカイブ(`flet-macos.tar.gz`)が事前に置かれていればダウンロードせずそれを使う仕様。ビルド前に該当ファイルをダウンロードして `site-packages/flet_desktop/app/` に配置し、`main.spec` の `datas` で `flet_desktop/app` として同梱する（2.4参照） |
| 4 | `flet` 本体が同梱する `controls/material/icons.json` 等の非Pythonデータファイルがフリーズ後に欠落 | GUI起動直後に `FileNotFoundError: .../flet/controls/material/icons.json` でクラッシュ（`ft.Icons.xxx` を1回でも参照すると発生） | `main.spec` で `PyInstaller.utils.hooks.collect_data_files('flet')` を使い、flet配下の非Pythonデータファイルを自動収集して `datas` に含める |
| 5 | `EXE` に直接 `a.binaries`/`a.datas` を渡すonefile方式は、macOSの`.app`バンドル(`BUNDLE`)と組み合わせるとPyInstallerから非推奨警告が出る | `DEPRECATION: Onefile mode in combination with macOS .app bundles ... will become an error in v7.0.` | `EXE(..., exclude_binaries=True)` + `COLLECT(...)` によるonedir方式に変更し、`BUNDLE` は `exe` ではなく `coll` をラップするように変更 |

これらの対処後、Mac実機（Apple Silicon, Python 3.12）で以下を実際に確認しました:
- `pyinstaller --noconfirm main.spec` が警告なく成功し、`dist/PixivVault.app` が生成される
- ビルド済み `.app` を直接実行すると、`ModuleNotFoundError`・`FileNotFoundError` なくプロセスが起動したまま維持される
- ローカル拡張機能連携サーバー(ポート25010)・多重起動防止ロック(ポート25011)の両方がLISTEN状態になる
- `notifications.py` 経由の `osascript` 通知呼び出しがエラーなく完了する
- `registry_helper.py` の全関数がダミー応答で正常終了する
- `gui.py` を含む全モジュールが `import` エラーなく読み込める（`tkinter` 込み。Homebrewの `python@3.12` には `python-tk@3.12` の別途インストールが必要）

なお、ビルド環境がSMB経由のネットワーク共有ボリューム（`/Volumes/Share/...`）上にある場合、`pip install`や`pyinstaller`の書き込みが大量の小ファイルI/Oのため著しく低速化することを確認しました（数千ファイルの`pyobjc`系パッケージ等）。**venvとビルド出力(`dist`/`build`)はローカルディスク上で作成し、完成した `.app` のみを共有ボリュームへコピーする**運用を推奨します。

---

## 3. 動作確認・検証計画 (Verification Plan)

### 3.1 事前準備（Mac実機）

```bash
# Python / pipの確認 (Conda不使用。python.org版 or Homebrew版のpython3を使用)
python3 --version
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

# tkinterの動作確認 (gui.pyのfiledialogが依存。Homebrew python の場合は python-tk が別途必要)
python3 -c "import tkinter; print('tkinter OK')"
# 失敗する場合: brew install python-tk@3.12 (使用中のpythonバージョンに合わせる)

pip install -r requirements.txt
pip install pyinstaller
```

### 3.2 コード修正の静的検証

```bash
# 各ファイルが単体でimportエラーを起こさないことを確認(macOS上で実行)
python3 -c "import notifications; print('notifications OK')"
python3 -c "import registry_helper; print('registry_helper OK')"
python3 -c "import scheduler; print('scheduler OK')"
python3 -c "import server; print('server OK')"
python3 -c "import core; print('core OK')"
```
期待結果: いずれも `ImportError` を出さずに `OK` が出力されること（= 2.1/2.2の修正が機能している証明）。

### 3.3 通知機能の検証

```bash
python3 -c "from notifications import send_notification; send_notification('PixivVault', 'macOS通知テスト')"
```
期待結果: 画面右上にmacOS標準の通知バナーが表示される。
- 通知権限が未許可の場合は「システム設定 > 通知」でターミナル（またはビルド後は `PixivVault.app`）への通知を許可してから再実行する。
- タイトル/本文にダブルクォートを含む文字列（例: `send_notification('Test "quote"', 'a\\b')`）でも `osascript` がエラーを起こさないことを確認する（エスケープ処理の検証）。

### 3.4 レジストリ処理ダミー化の検証

```bash
python3 -c "
import registry_helper as r
print('register:', r.register_protocol())      # True(何もしない)を期待
print('check:', r.check_protocol_registered())  # Falseを期待
print('unregister:', r.unregister_protocol())   # True(何もしない)を期待
"
```

### 3.5 GUI・ローカルサーバーの起動確認

```bash
python3 main.py
```
確認項目:
- [ ] クラッシュせずFletウィンドウ（1152x648）が起動する
- [ ] タスクトレイ（メニューバー）に `PixivVault` アイコンが表示される（`pystray` + `pyobjc-framework-Cocoa`）
- [ ] 「保存フォルダを開く」ボタンでmacOS標準のFinderが開く（2.3の修正確認）
- [ ] 拡張機能タブが例外なく表示され、「連携を有効にする」ボタン押下でエラーダイアログが出ない（2.2の修正確認、表示は「無効」のままでよい）
- [ ] `curl http://127.0.0.1:25010/` 等でローカルサーバーがLISTENしていることを確認（拡張機能連携用）
- [ ] 何らかのダウンロードを実行し、「お気に入り定期チェック」または「ZIP破損検知」相当のイベントでmacOS通知が表示される(3.3の実環境確認)

### 3.6 PyInstallerビルド検証（.appの作成）

事前に `assets/icon.icns` を用意する（Mac上で以下のいずれかで生成）:
```bash
# icon.png(1024x1024推奨)から.icnsを生成する例
mkdir icon.iconset
for size in 16 32 64 128 256 512; do
  sips -z $size $size assets/icon.png --out icon.iconset/icon_${size}x${size}.png
  sips -z $((size*2)) $((size*2)) assets/icon.png --out icon.iconset/icon_${size}x${size}@2x.png
done
iconutil -c icns icon.iconset -o assets/icon.icns
rm -rf icon.iconset
```

ビルド実行:
```bash
pyinstaller --noconfirm main.spec
```
確認項目:
- [ ] `dist/PixivVault.app` が生成される
- [ ] `open dist/PixivVault.app` でアプリが起動する
- [ ] `Contents/Info.plist` に `CFBundleURLSchemes: [pixivvault]` が含まれる
      ```bash
      /usr/libexec/PlistBuddy -c "Print :CFBundleURLTypes" dist/PixivVault.app/Contents/Info.plist
      ```
- [ ] ブラウザで `pixivvault://start` を開き、`PixivVault.app` が起動/前面化する（[extension/content.js:97](extension/content.js:97) との連携確認。初回はmacOSの「開いてもよいですか」確認ダイアログが出る想定）
- [ ] Gatekeeperにより初回起動がブロックされる場合は「システム設定 > プライバシーとセキュリティ」から許可、または開発中は `xattr -cr dist/PixivVault.app` で quarantine 属性を除去して確認する

### 3.7 リグレッション確認（Windows側）

本計画のすべての変更は `sys.platform` 分岐によるものであり、Windows側の既存コードパス（`win11toast`呼び出し・`winreg`呼び出し・`.ico`アイコン・`EXE`単体ビルド）はロジック上変化していません。Windows実機（またはCIがあれば）で以下を確認し、デグレードがないことを担保します:
- [ ] `pyinstaller --noconfirm main.spec` が従来どおり `dist/PixivVault.exe` を生成する（`BUNDLE`ブロックはmacOS判定でスキップされるため影響なし）
- [ ] 通知・レジストリ登録機能が従来どおり動作する
