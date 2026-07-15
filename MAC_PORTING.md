# PixivVault macOS 移植計画書 (MAC_PORTING.md)

本ドキュメントは、現在 Windows 向けに開発されている `PixivVault` を macOS (OS X) 環境でも動作するように移植するための対応計画およびやることリストをまとめたものです。

---

## 1. 移植に向けた主な課題と対応方針

Windows 特有のAPIやライブラリを使用している箇所を特定し、マルチプラットフォーム（Windows/macOS 両対応）化するための修正方針を以下に示します。

### ① 通知機能 (`win11toast`) のマルチプラットフォーム化
* **現状**: [server.py](server.py) や [main.py](main.py) で Windows 専用の `win11toast` を使用しています。
* **課題**: macOS 環境では `win11toast` がインポートできず、クラッシュします。
* **対応**: OS判定（`sys.platform`）を行い、macOS では macOS 標準の `osascript` を呼び出す形でデスクトップ通知を実装します。追加ライブラリが不要なため軽量です。
  ```python
  import sys
  import subprocess

  def send_notification(title, message):
      if sys.platform == "win32":
          try:
              from win11toast import toast
              toast(title, message, app_id="PixivVault")
          except Exception:
              pass
      elif sys.platform == "darwin":  # macOS
          cmd = f'display notification "{message}" with title "{title}"'
          try:
              subprocess.run(["osascript", "-e", cmd], check=True)
          except Exception:
              pass
  ```

### ② レジストリ処理 (`registry_helper.py`) の無効化・代替
* **現状**: [registry_helper.py](registry_helper.py) で `winreg` モジュールを使って Windows レジストリにカスタムURIスキーム（`pixivvault://`）を書き込んでいます。
* **課題**: macOS にはレジストリが存在せず、`winreg` モジュールもインポートできません。
* **対応**: 
  * macOS では、後述する PyInstaller ビルド時の `Info.plist`（`.app` バンドル設定）に URL スキームを記述することで、OS起動時やブラウザ連携時に自動的にアプリが起動するようになります。実行時のコードによるレジストリ登録は不要です。
  * `registry_helper.py` は、OSが Windows 以外の場合は `winreg` をインポートせず、ダミーの結果（常に `True` または何もしない）を返すように修正します。
  ```python
  import sys
  
  # Windows の場合のみ winreg をロード
  if sys.platform == "win32":
      import winreg
  else:
      winreg = None
  ```

### ③ パス区切り文字 (`\`) の標準化
* **現状**: `main.spec` などの一部で Windows 向けのバックスラッシュ（`\\`）がハードコードされています。
* **対応**: OS間でファイルパスの扱いを統一するため、`os.path.join` への置き換え、または `pathlib.Path` の利用、あるいは macOS でも解釈可能なフォワードスラッシュ（`/`）への置換を行います。

---

## 2. ビルド設定 (`main.spec`) の macOS 対応

PyInstaller のビルド設定ファイルを、OS判定によって動的に Windows 用と macOS 用の出力を切り替えられるように変更します。

### spec ファイルの構成変更案
```python
import sys
from PyInstaller.building.api import PYZ, EXE, COLLECT
from PyInstaller.building.build_main import Analysis

# macOS 向けには BUNDLE をインポート
if sys.platform == 'darwin':
    from PyInstaller.building.api import BUNDLE

# (Analysis などの共通定義はそのまま)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PixivVault',
    debug=False,
    console=False,
    icon='assets/icon.ico' if sys.platform == 'win32' else None, # Windows用
)

# macOS の場合は .app アプリケーションバンドルを作成
if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='PixivVault.app',
        icon='assets/icon.icns', # macOS用のアイコンファイル
        bundle_identifier='com.pixivvault.app',
        info_plist={
            'CFBundleURLTypes': [{
                'CFBundleURLName': 'PixivVault Protocol',
                'CFBundleURLSchemes': ['pixivvault']
            }]
        }
    )
```

---

## 3. 移植フェーズとやることリスト (TODO)

### 🟩 フェーズ 1: コードのOS依存切り離し
- [ ] `registry_helper.py` で `sys.platform` による条件分岐を入れ、macOS/Linux ではレジストリ操作をスキップしダミー応答を返すように修正する。
- [ ] `server.py` の `win11toast` インポートを try-except または OS判定で囲み、macOS では `osascript` を使った通知にフォールバックさせる。
- [ ] `main.py` 内の多重起動防止メッセージ通知部分も、マルチプラットフォーム対応の通知関数へ差し替える。

### 🟩 フェーズ 2: アセットの準備
- [ ] macOS用のアイコンフォーマット `.icns` を作成する（`assets/icon.png` を元に、Pillow または Mac 上の `iconutil` コマンドで作成可能）。

### 🟩 フェーズ 3: spec ファイルの拡張とビルドテスト
- [ ] `main.spec` を Windows / macOS 兼用 spec に書き換える。
- [ ] macOS 環境（Mac実機）上で、必要なパッケージ（`flet`, `requests`, `pystray`, `Pillow` など）をインストールした上で、`pyinstaller --noconfirm main.spec` を実行し、`dist/PixivVault.app` が作成されることを検証する。
