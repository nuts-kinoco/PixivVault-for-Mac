# Jules 向け調査・テスト依頼プロンプト

以下をそのまま Jules (https://jules.google.com) のタスクプロンプトとして貼り付けてください。
対象リポジトリ: `nuts-kinoco/PixivVault-for-Mac`

---

あなたは Python / PyInstaller / クロスプラットフォームデスクトップアプリに詳しいシニアエンジニアです。
このリポジトリ `PixivVault-for-Mac` は、Windows専用だったPixiv自動バックアップアプリ『PixivVault』を
macOS でも動作するように移植したものです。**ただしあなたの実行環境はLinuxの仮想環境であり、
最終ターゲットのmacOSそのものではない**ことを踏まえて調査してください（後述の「実行環境の制約」を参照）。

## リポジトリの前提知識

- GUIフレームワーク: **Flet 0.85.3固定**（`requirements.txt`参照）。バージョンを上げたり、0.85.3で非推奨/廃止のAPIを使わないこと。
- パッケージ管理: pip のみ。Conda は使用しない。
- `MAC_PORTING.md` に、Windows→macOS移植の背景と課題一覧がまとめてあるので、最初に読んでください。
- `main.spec` は手書きのPyInstaller specファイルで、`sys.platform`分岐によりWindows(.exe)とmacOS(.app/BUNDLE)の両方をビルドできる想定です。
- OS依存コードは `notifications.py`（通知）、`registry_helper.py`（Windowsレジストリ/URIスキーム）に分離されている想定です。

## 実行環境の制約（重要）

あなたのLinux VMでは以下ができません。無理に動かそうとせず、**静的解析・部分的な起動確認・ユニットテストレベルでの検証**に留めてください。
- macOS実機でのPyInstallerビルド（`.app`バンドル生成、`.icns`アイコン、`Info.plist`のURLスキーム登録）
- GUIウィンドウを実際に開いての目視確認（ディスプレイがない場合、`flet`の起動やpystrayのトレイアイコン表示がクラッシュする可能性があります。クラッシュ自体もバグ調査の対象にしてください＝「Linuxでの起動時に本来通らないはずのWindows/macOS専用コードパスに入って落ちていないか」を見る材料になります）
- 実際のPixivアカウントでのログイン・ダウンロード（Cookieやアカウント情報は提供されません。ネットワークが必要な処理は該当箇所をコードレビューで代替してください）

## 重点的にチェックしてほしいバグ懸念点

以下は移植時に特に事故りやすいポイントです。優先的に調査・検証し、確信度と再現手順を添えて報告してください。

1. **`sys.platform` 分岐の抜け漏れ**
   - `winreg`（Windows専用）や `win11toast`（Windows専用）を、`sys.platform == "win32"` のガードなしに直接 `import` している箇所が無いか、リポジトリ全体を `grep` で洗い出してください（特に `core.py`, `gui.py`, `server.py`, `main.py`, `scheduler.py` を重点的に）。
   - `registry_helper.py` の各関数（`register_protocol`, `unregister_protocol`, `check_protocol_registered`）が呼び出し元（`gui.py`, `main.py`）で「Windows専用ボタン」として適切に隠蔽/無効化されているか、それともmacOS/Linuxでもボタンが表示されて無意味な呼び出しをしてしまうか確認してください。

2. **`tray.py` のクロスプラットフォーム対応漏れ**
   - `tray.py` は `pystray` を `sys.platform` の分岐なしに使っています。macOSでは `pystray` は `pyobjc` バックエンドが必要（`requirements.txt` に `pyobjc-framework-Cocoa; sys_platform == "darwin"` はあるが、`pyobjc-framework-Quartz` 等の追加パッケージが要る場合があります）。実際に `pystray.Icon` の生成・`run_detached()` がmacOSで要求するpyobjc依存パッケージが requirements.txt に過不足なく揃っているか、pystrayのドキュメント/ソースを確認して指摘してください。
   - Linux環境でこのモジュールを import した際に何が起きるか（例外の種類、エラーメッセージ）を確認し、Windows/macOS以外での挙動が「クラッシュ」なのか「静かに失敗」なのかを明らかにしてください。

3. **パス処理のOS依存**
   - `\`（バックスラッシュ）のハードコードが残っていないか（`main.spec` 以外にも `core.py`, `gui.py`, `pixiv_client.py` 等の文字列結合・正規表現・保存先パス生成コードを確認）。
   - `sanitize_filename` 等のファイル名サニタイズ処理が、Windows予約文字だけでなくmacOS/Linuxのファイルシステムでも安全か（逆に、Windows専用の禁止文字だけを置換していて、実際は問題ないのに過剰にエスケープしていないか）。
   - `get_asset_path`（`main.py`, `gui.py` 内）が PyInstaller の `sys._MEIPASS`（onefile）と `onedir` ビルド両方、かつ開発時（非frozen）の3パターンで正しくassetsを解決できるか。`main.spec` は `onedir`(`COLLECT`) 前提になっているため、`_MEIPASS` を使う一部コードとビルド設定に矛盾がないか要確認。

4. **通知機能 (`notifications.py`) の堅牢性**
   - macOSの `osascript` 呼び出しで、`title`/`message` に含まれる可能性のある特殊文字（バッククォート、`$`、改行、絵文字、Pixivの作品タイトルに含まれがちな全角記号など）がコマンドインジェクションや構文エラーを起こさないか。現状の `_escape()` は `\` と `"` のみエスケープしていますが、それで十分か検証してください。
   - `osascript` が存在しない/失敗する環境（例: SSH経由のヘッドレスmacOSなど）でアプリ全体がクラッシュしないか（例外捕捉が適切か）。

5. **`main.spec` のビルド設定**
   - `argv_emulation=(sys.platform == 'darwin')` や `CFBundleURLTypes` の設定が、実際に `pixivvault://` カスタムURLスキームでの起動（Apple Event経由）を正しく`sys.argv`に橋渡しできる設定になっているか、PyInstaller/Fletの既知の制約と照らして確認してください。
   - `collect_data_files('flet')` や `flet_desktop` の同梱ロジックが、Flet 0.85.3のパッケージ構造と一致しているか（バージョン違いによりパスが変わっていないか）。

6. **拡張機能連携サーバー (`server.py`) のクロスプラットフォーム影響**
   - `server.py` はローカルHTTPサーバーとして `127.0.0.1` にバインドしていますが、cookies.txt/DBファイルへの相対パスアクセスなど、Windows以外での作業ディレクトリ（カレントディレクトリ）の扱いに差異が出ないか確認してください（例えば `.app` バンドルとして起動した場合、カレントディレクトリがユーザーの想定と異なる可能性があります）。

7. **依存パッケージのプラットフォーム条件**
   - `requirements.txt` の `sys_platform` マーカー（`win11toast; sys_platform == "win32"`, `pyobjc-framework-Cocoa; sys_platform == "darwin"`）が正しいマーカー構文か、`pip install -r requirements.txt` をLinux環境で実行してエラーなく完了するか実際に試してください（Windows専用/macOS専用パッケージがLinuxでスキップされることの確認も含む）。

## 実施してほしいこと

1. リポジトリを clone し、Linux仮想環境上で `pip install -r requirements.txt` を実行してエラーが出ないか確認する（ディスプレイが無いことに起因するFlet/pystray関連のエラーは許容し、それ以外の依存関係エラー・importエラーを重点的に洗い出す）。
2. 上記1〜7の懸念点について、実際にコードを読み、可能な範囲で（`python -c "import xxx"` 等の軽量な起動確認、`ast`/`py_compile`での構文チェック、あるいは疑わしい関数の単体呼び出しなど）検証を行う。
3. バグ・矛盾・移植漏れを見つけたら、深刻度（Critical/Major/Minor）付きで一覧化する。
4. 明確に再現・修正可能なものについては、修正PRを作成する。ただし **Flet 0.85.3 のAPI仕様を絶対に逸脱しない**こと、**Conda関連の記述を追加しない**ことを厳守してください。
5. macOS実機やディスプレイが無いために検証しきれなかった項目は「未検証」として明示し、憶測で「動作する/しない」と断定しないでください。

## 出力フォーマット

- `## 調査サマリー`: 見つかった問題の一覧表（ファイル名・行番号・深刻度・概要）
- `## 詳細`: 各問題について、再現手順または根拠コード、影響範囲、推奨修正方針
- `## 未検証項目`: Linux環境の制約で確認できなかった項目とその理由
- 修正を行った場合は、通常通りPRとして提出してください。
