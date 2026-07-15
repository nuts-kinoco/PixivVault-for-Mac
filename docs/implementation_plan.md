# PixivVault v2.0 実装計画書 & ロードマップ

## 📋 前提・コードベース評価

コードを実際に確認した結果、前回の計画書から修正・追記が必要な点を整理しました。

### 既存コードの重要な特性（引き継ぎ情報）
- **フォルダ構成**: 画像は `{save_path}/{作者名(user_id)}/{作品タイトル_p1.jpg}` の「**フラット構成**」（作品ごとのサブフォルダは現時点では**なし**）
- **`pixiv_client.py`** の `get_user_works` は `profile/all` → `profile/illusts` のAjax APIを利用
- **`core.py`** の `run_backup` は作品1件ずつループ処理。進捗コールバックは `(current, total)` 形式
- **小説API**: 現時点では `profile/all` の `novels` キーを取得しているが、詳細取得エンドポイントは未実装
- **`export_data`** で既に `shutil.make_archive` を使用する実績あり → F1のZip化は容易

### 修正点（前回計画からの変更）
1. ✅ **F1のZip化**: 仕様を確定。**作者フォルダ（`{save_path}/{作者名(user_id)}/`）を丸ごと `.zip` で圧縮**する方式。フォルダ構成の変更・作品単位のサブフォルダ化は**不要**。圧縮拡張子は `.zip` に固定（CBZ対応なし）。`export_data` での `shutil.make_archive` の実績があるため実装は容易。
2. ⚠️ **F3の常駐化**: Flet は `page.window.prevent_close` でウィンドウ閉じイベントを横取りできるため、`pystray` との組み合わせで実現可能。ただしパッケージ追加（`pystray`, `win11toast`）が必要。
3. ✅ **F2の残り時間**: `progress_callback` の引数を `(current, total, elapsed_sec)` に拡張し、GUI側で計算するシンプルな構成を採用。

---

## 🗺️ ロードマップ（リリース単位）

| フェーズ | バージョン | 内容 | 難易度 |
|---------|-----------|------|--------|
| **Phase 1** | **v2.0** | F1 (作者フォルダZip化) + F2 (残り時間表示) | 低 |
| **Phase 2** | **v2.1** | F4 (小説ダウンロード) | 中 |
| **Phase 3** | **v2.2** | F3 (自動巡回・常駐・♡ボタン・通知) | 中〜高 |
| **Phase 4** | **v3.0** | ブラウザ拡張機能との連携 | 高 |

---

## ✅ チェックリスト（フェーズ別・タスク別）

### Phase 1 — v2.0 : 作者フォルダZip化 & 残り時間表示

#### 1-A. ダウンロード完了後のZip圧縮機能（`core.py`）

> **仕様**: ユーザーごとのダウンロード完了時点で、画像をZIP圧縮する。
> 全体設定（Advanced）またはユーザーごとの個別設定（📦）が有効な場合に実行する。
> 新作追加時は、既存ZIPファイルへの「追記（Append）」を行う。

- `[x]` **1-A-1**: `core.py` に `append_to_zip(author_dir: str, zip_path: str)` ヘルパー関数を追加
  - フォルダ内のファイルを `zipfile` モジュールの `'a'` (Append) モードで追加し、追加が完了したファイルを削除する
  - 新規作成の場合は `shutil.make_archive` と同等の動きになるよう制御
- `[x]` **1-A-2**: `run_backup` / `run_batch_backup` の末尾で Zip化対象か判定するロジックを追加
  - 対象の条件: 「全体設定がON」または「その作者の個別Zip設定（📦）がON」
- `[x]` **1-A-3**: 対象の場合、`append_to_zip` を呼び出して一時フォルダからZipへファイルを移動させる
- `[x]` **1-A-4**: ダウンロード完了後以外でも手動で圧縮できるよう、「フォロー一覧」に「Zip再圧縮」ボタンを配置

#### 1-B. Zip化のUI対応（`gui.py`, `database.py`）

- `[x]` **1-B-1**: `database.py` の `following_users` テーブルに `is_zipped BOOLEAN DEFAULT 0` カラムを追加するマイグレーション
- `[x]` **1-B-2**: フォロー一覧タブの各ユーザーカードに「📦」トグルボタンを追加
  - クリック時に `db.set_zipped(user_id, status)` で個別Zip化状態を保存
- `[x]` **1-B-3**: 設定タブに `ft.ExpansionTile`（タイトル "Advanced / 高度な設定"）を追加し、その中に「すべての作者に対しダウンロード完了後zipにする」チェックボックス（デフォルトOFF）を配置
  - `settings` テーブルのキー `zip_all_after_download` で保存

#### 1-C. 残り時間の推定表示（`core.py` + `gui.py`）

- `[x]` **1-C-1**: `run_backup` の `progress_callback` シグネチャを `(current, total, elapsed_sec)` の3引数に拡張
  - 各作品処理の開始時刻を `time.perf_counter()` で計測し、処理完了後に経過秒を算出して渡す
- `[x]` **1-C-2**: `gui.py` の `handle_progress` 関数を修正
  - 渡された `elapsed_sec` を内部リスト（直近10件）に蓄積し、移動平均速度（件/秒）を計算
  - `remaining_count / avg_speed` → 「残り約XX分XX秒」or「残り約XX秒」に変換して表示
- `[x]` **1-C-3**: `gui.py` に `remaining_time_text = ft.Text("", size=12, color=ft.Colors.BLUE_200)` を追加
  - `ft.Row([progress_bar, progress_text, remaining_time_text])` の形でプログレスバー横に配置
- `[x]` **1-C-4**: バッチダウンロードの `batch_progress_callback` にも経過時間を渡して残り時間を表示
- `[x]` **1-C-5**: ダウンロード完了・停止時に `remaining_time_text.value = ""` でクリア

---

### Phase 2 — v2.1 : 小説ダウンロード対応

#### 2-A. Pixiv小説APIの調査・実装（`pixiv_client.py`）

- `[x]` **2-A-1**: `get_user_works` 内の `profile/all` レスポンスに含まれる `novels` キーを確認・取得
- `[x]` **2-A-2**: 小説詳細取得APIの調査（`/ajax/novel/{novel_id}` or `novel/series/{series_id}`）
  - エンドポイントの確認、レスポンスJSON構造の確認
- `[x]` **2-A-3**: `get_user_novels(self, user_id) -> List[Dict]` メソッドを追加
  - `profile/all` → `novels` キーの ID一覧取得 → 詳細取得（画像と同様の chunk 方式）
- `[x]` **2-A-4**: `get_novel_text(self, novel_id) -> dict` メソッドを追加
  - 本文テキスト (`content`)、シリーズ情報 (`series`)、タイトル、作者情報を返す
- `[x]` **2-A-5**: 小説内の挿絵（`[uploadedimage:xxxxx]` 記法）のURLを解決するロジックを追加

#### 2-B. 小説の保存処理（`core.py`）

- `[x]` **2-B-1**: `run_novel_backup(user_id, ...)` 関数（または `run_backup` に統合）を追加
- `[x]` **2-B-2**: テキストの前処理ロジックを実装
  - Pixiv独自記法（`[ruby:テキスト<rb>読み</rb>]` など）を整形 → ルビは `テキスト《読み》` 形式に変換
  - 挿絵記法 `[uploadedimage:XXXXX]` → `[挿絵: https://...]` に変換（またはダウンロードして同梱）
  - Unicode絵文字（💗など）はそのままUTF-8で保存（Windows標準テキストエディタは対応済み）
- `[x]` **2-B-3**: 各小説をフォルダ内に `{novel_id}_{safe_title}.txt` として UTF-8 BOM付きで保存
  - BOM付きにすることでメモ帳でも文字化けなく開ける
- `[x]` **2-B-4**: 挿絵画像を `{フォルダ}/images/` サブディレクトリにダウンロード
- `[x]` **2-B-5**: DBの `works` テーブルに `content_type` カラム（`illust` / `novel`）を追加し、小説の管理も一元化

#### 2-C. 小説のUI対応（`gui.py`）

- `[x]` **2-C-1**: タブ1（個別ダウンロード）に「**イラスト/漫画**・**小説**・**両方**」の実行対象を選択するラジオボタン or ドロップダウンを追加
- `[x]` **2-C-2**: フォロー一覧タブの一括ダウンロードでも同様の対象選択を追加

---

### Phase 3 — v2.2 : 自動巡回・常駐・通知・♡機能

#### 3-A. DBの拡張（`database.py`）

- `[ ]` **3-A-1**: `following_users` テーブルに `is_favorite INTEGER DEFAULT 0` カラムを追加するマイグレーション
  - 既存DBに `ALTER TABLE following_users ADD COLUMN is_favorite INTEGER DEFAULT 0` を `try/except` で追加
- `[ ]` **3-A-2**: `get_favorite_users() -> List[Dict]` メソッドを追加
- `[ ]` **3-A-3**: `set_favorite(user_id: str, value: bool)` メソッドを追加

#### 3-B. 自動巡回のスケジューラ（新規 `scheduler.py`）

- `[ ]` **3-B-1**: `scheduler.py` を新規作成
  - `schedule` ライブラリ または `threading.Timer` で指定間隔のジョブ管理
- `[ ]` **3-B-2**: 巡回ジョブ関数 `check_and_download_favorites()` を実装
  - `db.get_favorite_users()` → `run_backup` を差分モードで実行
  - 新着があった場合は通知キューに積む
- `[ ]` **3-B-3**: 設定テーブルに `auto_check_interval_hours` キーを追加
- `[ ]` **3-B-4**: アプリ起動時（`main.py`）にスケジューラをバックグラウンドスレッドとして起動

#### 3-C. Windows通知（新規 or `scheduler.py` 内）

- `[ ]` **3-C-1**: `win11toast` パッケージを採用。`requirements.txt` に追加
- `[ ]` **3-C-2**: `notify(title, body)` ラッパー関数を実装
  - 新着作品が N 件あった場合: 「💕 ○○さんの新着作品が N 件あります」のようなメッセージ
- `[ ]` **3-C-3**: 通知クリック時にアプリウィンドウを前面に出す処理

#### 3-D. タスクトレイ常駐（`main.py` + `tray.py`）

- `[ ]` **3-D-1**: `pystray` パッケージを採用。`requirements.txt` に追加
- `[ ]` **3-D-2**: `tray.py` を新規作成
  - システムトレイアイコン（PixivVaultアイコン）の設定
  - 右クリックメニュー: 「開く」「今すぐ巡回」「終了」
- `[ ]` **3-D-3**: `main.py` の `ft.app()` 終了後もトレイが残るよう `target` と `view` を調整
- `[ ]` **3-D-4**: `page.window.prevent_close` を設定し、✕ボタンでウィンドウを「非表示」にする（終了ではなく隠す）
- `[ ]` **3-D-5**: 「終了」メニューでトレイアイコンを削除し、完全にプロセスを終了する

#### 3-E. フォロー一覧UIへの♡ボタン追加（`gui.py`）

- `[ ]` **3-E-1**: フォロー一覧の各ユーザーカードに「♡」トグルボタンを追加
- `[ ]` **3-E-2**: ♡ボタン押下時に `db.set_favorite()` を呼び出し、状態をDBに保存
- `[ ]` **3-E-3**: カード表示時に DB からお気に入り状態を読み込み、♡ボタンの初期状態を反映
- `[ ]` **3-E-4**: 設定タブに「自動巡回の間隔」設定項目（Dropdown or TextField）を追加
  - 選択肢: 6時間 / 12時間 / 24時間（毎日） / 48時間（2日毎）

---

### Phase 4 — v3.0 : ブラウザ拡張機能との連携

> [!NOTE]
> このフェーズは独立した設計が必要なため、詳細計画は Phase 3 完了後に別途立案します。

- `[ ]` **4-1**: PixivVault アプリにローカルHTTPサーバー（FastAPI or Flask）を内蔵する設計調査
- `[ ]` **4-2**: 拡張機能（Chrome Extension）のマニフェスト・コンテンツスクリプト設計
- `[ ]` **4-3**: フォロー一覧ページへのダウンロード・♡ボタン注入
- `[ ]` **4-4**: ユーザー個別ページ (`/users/{id}`) へのボタン注入

---

## 📦 必要パッケージ一覧（追加分）

| パッケージ | 用途 | フェーズ |
|-----------|------|---------|
| （追加なし） | F1, F2は標準ライブラリのみ | Phase 1 |
| （追加なし） | F4の小説保存も標準ライブラリのみ | Phase 2 |
| `pystray` | タスクトレイ常駐 | Phase 3 |
| `win11toast` | Windowsトースト通知 | Phase 3 |
| `schedule` (任意) | 定期実行スケジューラ | Phase 3 |
| `fastapi` + `uvicorn` | 拡張機能との通信用ローカルサーバー | Phase 4 |

---

## ⚠️ 重要な設計上の決定事項（承認をお願いします）

> [!IMPORTANT]
> **Q1: フォルダ構成の変更について（Phase 1-A）**
>
> 現在の保存先は `Images/{作者名(ID)}/作品タイトル_p1.jpg`（フラット）ですが、
> CBZ化のため `Images/{作者名(ID)}/{作品ID_タイトル}/p001.jpg` に変更します。
> **既存のダウンロード済み画像の移行（マイグレーション）をどうするか決定が必要です。**
> - A案: 初回起動時に自動で旧→新フォルダ構成に移行する（時間がかかる可能性あり）
> - B案: 移行ボタンを設定タブに設ける（手動）
> - C案: 旧フォルダ構成はそのまま残し、**新規ダウンロードから**新構成を適用する（最も安全）

> [!IMPORTANT]
> **Q2: CBZ化のデフォルト動作（Phase 1-B）**
>
> 設定のデフォルト値をどうするか:
> - A案: デフォルトで `.cbz` に圧縮（HoneyView利用想定）
> - B案: デフォルトは「フォルダのまま」として、設定でCBZに切り替える

> [!CAUTION]
> **Q3: 小説のルビ記法について（Phase 2-B）**
>
> Pixivの小説には独自記法（`[[rb:漢字 > 読み]]` など）が含まれます。
> `.txt` 形式ではルビの表示は不可能なので `テキスト（読み）` のように変換するか、
> 将来的に `.epub` 形式への対応も検討すべきか判断をお願いいたします。
