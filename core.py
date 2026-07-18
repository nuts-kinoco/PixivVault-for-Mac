import os
import re
import json
import shutil
import logging
import time
import zipfile
import threading
from datetime import datetime
from database import Database
from pixiv_client import PixivClient
import epub_builder
import io
from PIL import Image

class StopRequested(Exception):
    """ユーザーが停止操作を行ったことを示す例外。check_state() から送出される。

    save_ugoira() の MP4変換処理はOpenCV/imageio呼び出し失敗をラップするため広い
    except Exception を使っているが、これがユーザーの停止要求まで誤って握りつぶし
    GIF形式へフォールバックしてしまわないよう、この例外だけは明示的に再送出する。
    """
    pass

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', '_', name)

# 作者(user_id)単位のロック。GUI手動実行・スケジューラ定期チェック・拡張機能連携キューの
# 3経路が同じ作者のダウンロード/ZIP追記を同時に行うと、zipfileはファイルロックをしないため
# ZIPアーカイブの中央ディレクトリが競合破損する。そのため同一作者の処理は必ず直列化する。
_user_locks = {}
_user_locks_guard = threading.Lock()

def get_user_lock(user_id) -> threading.Lock:
    key = str(user_id)
    with _user_locks_guard:
        lock = _user_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _user_locks[key] = lock
        return lock

def notify_toast(db: Database, title: str, body: str):
    """OS通知(トースト)を送信する。scheduler.py/server.pyの通知と同じ設定キー(enable_notifications)を使う。"""
    if db.get_setting("enable_notifications", "1") != "1":
        return
    try:
        from notifications import send_notification
        threading.Thread(target=lambda: send_notification(title, body), daemon=True).start()
    except Exception as e:
        logging.getLogger(__name__).error(f"通知エラー: {e}")

def _verify_zip_and_notify(db: Database, zip_path: str, label: str, log, alert=None):
    """ZIP追記直後に整合性を検証し、破損していればログ・アラート・OS通知で知らせる。
    (DBのwork/failed_queueは自動変更しない。再ダウンロードするかはユーザーが判断する想定)"""
    if not os.path.exists(zip_path):
        return
    log(f"ZIPの整合性を検証しています: {label} (サイズが大きい場合、時間がかかることがあります)")
    is_valid, reason = verify_work_integrity(zip_path, is_zip=True)
    if is_valid:
        return
    msg = f"ZIP破損を検出しました: {label} ({reason})"
    log(msg, "ERROR")
    if alert:
        alert(msg)
    notify_toast(db, "PixivVault - ZIP破損検出", f"{label} のアーカイブに問題があります: {reason}")

# 【魔法の解放】Pythonの zipfile.ZIP64_LIMIT は標準で2GB(0x7FFFFFFF)に設定されているため、
# 2GB超~4GB未満のサイズ・オフセットでも allowZip64=False 時に例外が出たり、
# allowZip64=True 時に途中からZip64に切り替わるキメラZIPを生成してしまう。
# 本来のZIP規格上限である4GB(0xFFFFFFFF)へと解放してキメラ構造の発生を永久防止する。
try:
    zipfile.ZIP64_LIMIT = 0xFFFFFFFF
except Exception:
    pass

def _has_zip64_extra(extra: bytes) -> bool:
    """ZipInfo.extra (拡張フィールド) 内にZip64拡張情報ヘッダ(header_id=1)が含まれるか判定する。"""
    pos = 0
    while pos + 4 <= len(extra):
        header_id = int.from_bytes(extra[pos:pos + 2], 'little')
        data_size = int.from_bytes(extra[pos + 2:pos + 4], 'little')
        if header_id == 1:
            return True
        pos += 4 + data_size
    return False

def normalize_zip_for_compatibility(zip_path: str, log_callback=None):
    """
    ZIPファイルのサイズが大きな場合（例えば1.8GB以上）や追記によって
    標準32bitヘッダとZip64拡張ヘッダが途中混在するキメラ構造になった場合、
    またはパス区切りの異常がある場合に、全エントリを一貫したフォーマットへ自動正規化します。
    """
    if not os.path.exists(zip_path):
        return

    temp_path = f"{zip_path}.temp_normalize"
    try:
        size = os.path.getsize(zip_path)
        # 1.8 GiB (1,932,735,283 bytes) 以上、またはパスセパレータ異常や途中混在キメラを検出した場合にリビルド
        needs_normalize = (size > 1800 * 1024 * 1024)

        if not needs_normalize:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                has_std = False
                has_zip64 = False
                for info in zf.infolist():
                    if '\\' in info.filename:
                        needs_normalize = True
                        break
                    is_64 = (info.header_offset > 2147483647) or _has_zip64_extra(info.extra)
                    if is_64:
                        has_zip64 = True
                    else:
                        has_std = True
                    if has_std and has_zip64:
                        needs_normalize = True
                        break

        if not needs_normalize:
            return

        # 約3.9 GiB (4000*1024*1024 = 4,194,304,000 bytes) 未満であれば allowZip64=False を厳格に指定し、
        # 前半が標準32bit・後半がZip64となるキメラ状態を完全に回避した純粋32bit書庫にする
        use_zip64 = (size >= 4000 * 1024 * 1024)
        if log_callback:
            mode_str = "Zip64一貫モード" if use_zip64 else "純粋32bit一貫モード"
            log_callback(f"ZIP書庫の正規化（キメラ構造防止・{mode_str}化）を実行します: {os.path.basename(zip_path)}", "INFO")

        with zipfile.ZipFile(zip_path, 'r') as src_zip, \
             zipfile.ZipFile(temp_path, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=use_zip64) as dst_zip:
            for info in src_zip.infolist():
                file_data = src_zip.read(info.filename)
                new_filename = info.filename.replace('\\', '/')
                new_info = zipfile.ZipInfo(filename=new_filename, date_time=info.date_time)
                new_info.compress_type = info.compress_type
                new_info.external_attr = info.external_attr
                new_info.flag_bits = info.flag_bits
                dst_zip.writestr(new_info, file_data)

        os.replace(temp_path, zip_path)
        if log_callback:
            log_callback(f"ZIP書庫の正規化が正常に完了しました: {os.path.basename(zip_path)}", "INFO")
    except Exception as e:
        if log_callback:
            log_callback(f"ZIP書庫の正規化中に警告: {e}", "WARNING")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def append_to_zip(author_dir: str, zip_path: str, log_callback=None):
    """ディレクトリ内のファイルをZIPファイルの末尾に追加し、追加した元ファイルを削除します"""
    if not os.path.exists(author_dir):
        return

    try:
        if log_callback:
            log_callback(f"Zipファイルに追加しています: {os.path.basename(zip_path)}", "INFO")

        # 既存のZIP内エントリ名を取得しておき、同名ファイルの二重追加を防ぐ
        # (誤判定による再ダウンロードなどで同じファイルが渡された場合でも重複させない)
        existing_names = set()
        if os.path.exists(zip_path):
            with zipfile.ZipFile(zip_path, 'r') as existing_zip:
                existing_names = set(name.replace('\\', '/') for name in existing_zip.namelist())

        # 4GB未満の書庫では allowZip64=False に設定し、追記による途中切り替えキメラ化を防ぐ
        use_zip64 = False
        if os.path.exists(zip_path) and os.path.getsize(zip_path) >= 4000 * 1024 * 1024:
            use_zip64 = True

        # 'a' (Append) モードでZIPを開く。ファイルが無い場合は自動作成される。
        with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED, allowZip64=use_zip64) as zipf:
            for root, _, files in os.walk(author_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    # パス区切り文字を必ずフォワードスラッシュに正規化して書き込む
                    arcname = os.path.relpath(file_path, author_dir).replace('\\', '/')
                    if arcname in existing_names:
                        if log_callback:
                            log_callback(f"Zip内に同名ファイルが既に存在するためスキップします: {arcname}", "DEBUG")
                        continue
                    zipf.write(file_path, arcname)
                    existing_names.add(arcname)

        # ZIP追加が成功したら元フォルダを削除
        shutil.rmtree(author_dir)
        if log_callback:
            log_callback(f"Zip化が完了し、一時フォルダを削除しました。", "INFO")

        # 追記後に、書庫サイズやキメラ化の有無を点検し、必要であれば自動正規化を実施
        normalize_zip_for_compatibility(zip_path, log_callback)

    except Exception as e:
        if log_callback:
            log_callback(f"Zip化に失敗しました: {e}", "ERROR")

def verify_work_integrity(target_path: str, is_zip: bool = False, expected_page_count: int = None, file_paths: list = None) -> tuple[bool, str]:
    """保存された作品ファイルまたはディレクトリの品質検証を実行します。

    file_paths が指定された場合、target_path 配下を丸ごと走査するのではなく、
    このリストに含まれるファイルのみを検証対象にします。
    (target_path が作品専用フォルダではなく、同じ作者の他作品と共有しているフォルダである場合に
    フォルダ全体を誤って対象にしてしまわないようにするためのモードです)

    戻り値: (is_valid: bool, error_reason: str or None)
    """
    if file_paths is not None:
        if not file_paths:
            return False, "保存されたファイルが見つかりません"
        image_files = []
        for file_p in file_paths:
            if not os.path.exists(file_p):
                return False, f"ファイルが見つかりません ({os.path.basename(file_p)})"
            if os.path.getsize(file_p) == 0:
                return False, f"0バイトファイル検出 ({os.path.basename(file_p)})"
            if os.path.splitext(file_p)[1].lower() in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4"]:
                image_files.append(file_p)
        if any(os.path.splitext(fp)[1].lower() in [".gif", ".mp4"] for fp in file_paths):
            return True, None
        if expected_page_count and expected_page_count > 0 and len(image_files) < expected_page_count:
            return False, f"ページ不足 (期待値:{expected_page_count} 実際の枚数:{len(image_files)})"
        return True, None

    if not os.path.exists(target_path):
        return False, "ファイルまたはディレクトリが存在しません"

    if is_zip or target_path.lower().endswith(".zip") or target_path.lower().endswith(".epub"):
        size = os.path.getsize(target_path)
        if size == 0:
            return False, "0バイトファイル検出"
        try:
            with zipfile.ZipFile(target_path, 'r') as zf:
                bad_file = zf.testzip()
                if bad_file is not None:
                    return False, f"ZIP破損検出 (不正ファイル: {bad_file})"
                if len(zf.namelist()) == 0:
                    return False, "アーカイブ内部が空です"
        except Exception as e:
            return False, f"ZIP破損検出 ({str(e)})"
        return True, None

    if os.path.isdir(target_path):
        image_files = []
        for root, _, files in os.walk(target_path):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".txt", ".epub", ".mp4"]:
                    file_p = os.path.join(root, file)
                    if os.path.getsize(file_p) == 0:
                        return False, f"0バイトファイル検出 ({file})"
                    if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4"]:
                        image_files.append(file_p)

        if expected_page_count and expected_page_count > 0:
            if any(fp.lower().endswith(".gif") or fp.lower().endswith(".mp4") for fp in image_files):
                return True, None
            if len(image_files) < expected_page_count:
                return False, f"ページ不足 (期待値:{expected_page_count} 実際の枚数:{len(image_files)})"
        return True, None

    if os.path.getsize(target_path) == 0:
        return False, "0バイトファイル検出"
    return True, None

def save_ugoira(
    client: PixivClient,
    work_id: str,
    title: str,
    user_id: str,
    user_name: str,
    work_img_dir: str,
    use_work_folder: bool,
    safe_title: str,
    check_state_cb,
    log_cb,
    db: Database,
    ugoira_save_format: str
) -> tuple[bool, str, list]:
    """うごイラ作品のダウンロードおよび指定されたフォーマットへの保存を行います。

    戻り値: (is_ok: bool, error_reason: str or None, saved_file_paths: list)
    """
    check_state_cb()
    meta = client.get_ugoira_meta(work_id)
    zip_url = meta.get("originalSrc")
    frames = meta.get("frames", [])
    if not zip_url or not frames:
        return False, "うごイラのメタデータ(フレーム遅延情報等)を取得できませんでした", []

    check_state_cb()
    log_cb(f"うごイラZIPをダウンロード中... (コマ数: {len(frames)})", "DEBUG")
    try:
        zip_data = client.download_ugoira_zip_data(zip_url)
    except Exception as e:
        return False, f"うごイラZIPダウンロード失敗 ({e})", []

    check_state_cb()
    zip_buffer = io.BytesIO(zip_data)
    durations = [int(f.get("delay", 100)) for f in frames]

    saved_file_paths = []

    try:
        with zipfile.ZipFile(zip_buffer) as zf:
            if ugoira_save_format == "folder":
                if use_work_folder:
                    ugoira_dir = work_img_dir
                else:
                    ugoira_dir = os.path.join(work_img_dir, f"{safe_title[:30]}({work_id})")
                os.makedirs(ugoira_dir, exist_ok=True)

                for frame in frames:
                    check_state_cb()
                    file_name = frame["file"]
                    extracted_path = os.path.join(ugoira_dir, file_name)
                    with zf.open(file_name) as src, open(extracted_path, "wb") as dst:
                        dst.write(src.read())
                    saved_file_paths.append(extracted_path)

                json_path = os.path.join(ugoira_dir, "ugoira_meta.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump({"frames": frames}, f, indent=4, ensure_ascii=False)
                saved_file_paths.append(json_path)

                log_cb(f"うごイラを展開フォルダに保存しました: {os.path.basename(ugoira_dir)}", "DEBUG")
                return True, None, saved_file_paths

            pil_images = []
            for frame in frames:
                check_state_cb()
                file_name = frame["file"]
                with zf.open(file_name) as img_file:
                    img = Image.open(img_file).convert("RGBA")
                    pil_images.append(img)

            if not pil_images:
                return False, "うごイラZIP内に画像が存在しませんでした", []

            if ugoira_save_format == "mp4":
                mp4_filename = f"{safe_title}.mp4" if use_work_folder else f"{safe_title}_{work_id}.mp4"
                mp4_path = os.path.join(work_img_dir, mp4_filename)

                mp4_success = False
                try:
                    import cv2
                    import numpy as np
                    avg_delay = sum(durations) / max(len(durations), 1)
                    fps = max(1.0, min(60.0, 1000.0 / max(avg_delay, 10)))

                    height, width = pil_images[0].height, pil_images[0].width
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                    out = cv2.VideoWriter(mp4_path, fourcc, fps, (width, height))
                    if out.isOpened():
                        for img in pil_images:
                            check_state_cb()
                            bgr_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGBA2BGR)
                            out.write(bgr_img)
                        out.release()
                        mp4_success = os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0
                except StopRequested:
                    raise
                except Exception as e_mp4:
                    log_cb(f"OpenCVによるMP4変換に失敗しました: {e_mp4}", "DEBUG")

                if not mp4_success:
                    try:
                        import imageio
                        import numpy as np
                        avg_delay = sum(durations) / max(len(durations), 1)
                        fps = max(1.0, min(60.0, 1000.0 / max(avg_delay, 10)))
                        with imageio.get_writer(mp4_path, fps=fps, codec='libx264') as writer:
                            for img in pil_images:
                                check_state_cb()
                                writer.append_data(np.array(img.convert("RGB")))
                        mp4_success = os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0
                    except StopRequested:
                        raise
                    except Exception as e_io:
                        log_cb(f"imageioによるMP4変換に失敗しました: {e_io}", "DEBUG")

                if mp4_success:
                    log_cb(f"うごイラのMP4保存に成功しました: {mp4_filename}", "DEBUG")
                    return True, None, [mp4_path]
                else:
                    log_cb("MP4変換用ライブラリがないか失敗したため、GIF形式で自動保存します。", "WARNING")

            gif_filename = f"{safe_title}.gif" if use_work_folder else (f"{safe_title}.gif" if not os.path.exists(os.path.join(work_img_dir, f"{safe_title}.gif")) else f"{safe_title}_{work_id}.gif")
            gif_path = os.path.join(work_img_dir, gif_filename)
            first_img = pil_images[0]
            first_img.save(
                gif_path,
                format="GIF",
                save_all=True,
                append_images=pil_images[1:],
                duration=durations,
                loop=0,
                disposal=2
            )
            log_cb(f"うごイラのGIF保存に成功しました: {gif_filename}", "DEBUG")
            return True, None, [gif_path]

    except StopRequested:
        raise
    except Exception as e:
        return False, f"うごイラ変換処理中にエラーが発生しました ({e})", []

def run_backup(user_id: str, client: PixivClient, db: Database, is_full: bool = False, target_type: str = "both", log_callback=None, progress_callback=None, alert_callback=None, stop_event=None, pause_event=None, new_only: bool = False):
    """作者(user_id)単位で排他制御しながらバックアップを実行する。

    GUI手動実行・スケジューラ定期チェック・拡張機能連携キューが同じ作者を同時に処理し、
    ZIPアーカイブへの同時書き込みで破損させることを防ぐため、実処理は _run_backup_impl に委譲し、
    ここで作者単位のロックを取得する。
    """
    with get_user_lock(user_id):
        return _run_backup_impl(
            user_id, client, db, is_full=is_full, target_type=target_type,
            log_callback=log_callback, progress_callback=progress_callback,
            alert_callback=alert_callback, stop_event=stop_event, pause_event=pause_event,
            new_only=new_only
        )

def _run_backup_impl(user_id: str, client: PixivClient, db: Database, is_full: bool = False, target_type: str = "both", log_callback=None, progress_callback=None, alert_callback=None, stop_event=None, pause_event=None, new_only: bool = False):
    logger = logging.getLogger(__name__)
    
    def log(msg, level="INFO"):
        if level == "INFO":
            logger.info(msg)
        elif level == "WARNING":
            logger.warning(msg)
        elif level == "ERROR":
            logger.error(msg)
        elif level == "DEBUG":
            logger.debug(msg)
            return
        
        if log_callback:
            log_callback(msg)
            
    def alert(msg):
        logger.warning(msg)
        if alert_callback:
            alert_callback(msg)
        elif log_callback:
            log_callback(f"⚠️ {msg}")

    def check_state():
        if stop_event and stop_event.is_set():
            raise StopRequested("処理がユーザーによって中止されました。")
        if pause_event and pause_event.is_set():
            log("処理を一時停止しています...", "INFO")
            while pause_event.is_set():
                if stop_event and stop_event.is_set():
                    raise StopRequested("処理がユーザーによって中止されました。")
                pause_event.wait(timeout=1.0)
            log("処理を再開します。", "INFO")

    def early_exit_checker(parsed_work):
        if not new_only:
            return False
        db_record = db.get_work(str(parsed_work['id']))
        if db_record and db_record['update_date'] == parsed_work['update_date'] and not db_record['is_deleted']:
            return True
        return False

    check_state()
    stats = {
        "new_count": 0,
        "updated_count": 0,
        "deleted_count": 0,
        "restored_count": 0,
        "skipped_count": 0,
        "failed_count": 0
    }
    # 作者ごとのオーバーライド設定の確認
    override_target_type = db.get_author_setting(user_id, "target_type", None)
    if override_target_type and override_target_type not in ("", "default"):
        target_type = override_target_type
        log(f"作者個別設定が適用されました (対象タイプ: {target_type})", "INFO")

    # 【フェーズA】Pixivから最新の作品一覧を取得
    log(f"【フェーズA】Pixivから最新の作品一覧({target_type})を取得します。")
    current_works = []
    known_ids = set()
    if target_type in ("illust", "both"):
        current_works.extend(client.get_user_works(user_id, early_exit_checker=early_exit_checker, known_ids_out=known_ids))
    if target_type in ("novel", "both"):
        current_works.extend(client.get_user_novels(user_id, early_exit_checker=early_exit_checker, known_ids_out=known_ids))

    if not current_works:
        log("保存対象の作品がありませんでした。")
        # return せずに後続のZIP化処理等へ進む

    # 詳細情報の取得に一時的に失敗したIDも「削除ではない」とみなすため、
    # 一覧取得(profile/all)で存在が確認できた known_ids も現存扱いに含める
    current_work_ids = {str(w['id']) for w in current_works} | known_ids
    
    check_state()
    # 【フェーズB】削除検知 (全件一覧を取得するモードの場合のみ実行)
    if not new_only:
        log("【フェーズB】DBと比較し、Pixivから削除された作品がないか検知します。")
        # target_type が絞り込まれている場合（作者個別設定含む）、今回取得していない
        # content_type の既存作品まで「削除された」と誤検知しないよう、DB側の比較対象も
        # 同じ target_type に限定する。
        content_types = None
        if target_type == "illust":
            content_types = ["illust"]
        elif target_type == "novel":
            content_types = ["novel"]
        db_work_ids = db.get_user_work_ids(user_id, content_types=content_types)
        deleted_ids = db_work_ids - current_work_ids
        
        for d_id in deleted_ids:
            work_record = db.get_work(d_id)
            if work_record and not work_record['is_deleted']:
                db.mark_as_deleted(d_id)
                stats["deleted_count"] += 1
                alert(f"以下の作品がPixivから削除されている可能性があります: {work_record['title']} (ID: {d_id})")
                
    check_state()
    # 【フェーズC】新規・更新分（または全件）ダウンロード
    if current_works:
        log("【フェーズC】画像のダウンロードとDBの更新を行います。")
    base_img_dir = db.get_setting("save_path", "Images")
    novel_save_format = db.get_setting("novel_save_format", "epub")
    ugoira_save_format = db.get_setting("ugoira_save_format", "gif")
    use_work_folder = db.get_setting("use_work_folder", "0") == "1"
    total = len(current_works)
    # 今回のダウンロード対象が0件（または全件が既に最新）の場合でも、
    # 後段のZIP化判定でユーザー名（フォルダ名の特定）に使えるよう、
    # 見つかった作品からユーザー名を控えておく。
    resolved_user_name = None

    start_time = time.perf_counter()

    for idx, work in enumerate(current_works, 1):
        check_state()
        if progress_callback:
            elapsed = time.perf_counter() - start_time
            progress_callback(idx, total, elapsed)
            
        work_id = str(work['id'])
        title = work.get('title', '無題')
        user_name = work.get('user_name', 'Unknown')
        if user_name and user_name != 'Unknown':
            resolved_user_name = user_name
        page_count = work.get('page_count', 1)
        create_date = work.get('create_date', '')
        update_date = work.get('update_date', '')

        db_record = db.get_work(work_id)
        needs_download = False
        
        status_type = "skipped"
        if is_full:
            log(f"[{idx}/{total}] 完全チェック: ID: {work_id} 「{title}」")
            needs_download = True
            if not db_record:
                status_type = "new"
            elif db_record['update_date'] != update_date:
                status_type = "updated"
            else:
                status_type = "skipped"
        else:
            if not db_record:
                log(f"[{idx}/{total}] [〇新規] 作品を発見！ ID: {work_id} 「{title}」")
                needs_download = True
                status_type = "new"
            elif db_record['update_date'] != update_date:
                log(f"[{idx}/{total}] [△更新] 作品を発見！ ID: {work_id} 「{title}」")
                needs_download = True
                status_type = "updated"
            elif db_record['is_deleted']:
                log(f"[{idx}/{total}] [↺復帰] 削除状態から復帰した作品を発見！ ID: {work_id} 「{title}」")
                needs_download = True
                status_type = "restored"
            else:
                stats["skipped_count"] += 1
                continue
            
        if needs_download:
            try:
                work_type = work.get('type', 'illust')
                safe_title = sanitize_filename(title)
                safe_user_name = sanitize_filename(user_name)
                author_dir_name = f"{safe_user_name}({user_id})"
                work_img_dir = os.path.join(base_img_dir, author_dir_name)
                if use_work_folder:
                    work_folder_name = f"{safe_title[:30]}({work_id})"
                    work_img_dir = os.path.join(work_img_dir, work_folder_name)
                os.makedirs(work_img_dir, exist_ok=True)
                
                if work_type == 'novel':
                    novel_data = client.get_novel_text(work_id)
                    raw_content = novel_data.get('content', '')
                    
                    # 挿絵の処理とダウンロード
                    embedded_images = novel_data.get('textEmbeddedImages', {})
                    epub_embedded_images = []
                    txt_content = raw_content
                    html_content = raw_content
                    
                    if embedded_images:
                        novel_img_dir = os.path.join(work_img_dir, "images")
                        os.makedirs(novel_img_dir, exist_ok=True)
                        for img_id, img_info in embedded_images.items():
                            original_url = img_info.get('urls', {}).get('original')
                            if original_url:
                                ext = os.path.splitext(original_url.split('?')[0])[1] or '.jpg'
                                img_filename = f"{img_id}{ext}"
                                save_path = os.path.join(novel_img_dir, img_filename)
                                if not os.path.exists(save_path):
                                    client.download_image(original_url, save_path)
                                
                                # TXT用
                                txt_content = txt_content.replace(f"[uploadedimage:{img_id}]", f"[挿絵: images/{img_filename}]")
                                # HTML (EPUB) 用
                                html_content = html_content.replace(f"[uploadedimage:{img_id}]", f'<img src="../Images/{img_id}{ext}" alt="挿絵" />')
                                epub_embedded_images.append({'id': img_id, 'path': save_path, 'ext': ext})
                    
                    # ルビの変換
                    # TXT用: [[rb:漢字 > かんじ]] → 漢字《かんじ》
                    txt_content = re.sub(r'\[\[rb:(.*?) > (.*?)\]\]', r'\1《\2》', txt_content)
                    # HTML用: [[rb:漢字 > かんじ]] → <ruby>漢字<rt>かんじ</rt></ruby>
                    html_content = re.sub(r'\[\[rb:(.*?) > (.*?)\]\]', r'<ruby>\1<rt>\2</rt></ruby>', html_content)
                    
                    # Pixiv独自の改ページタグ [newpage] を処理
                    txt_content = txt_content.replace('[newpage]', '\n\n---\n\n')
                    html_content = html_content.replace('[newpage]', '<hr/>')
                    
                    # HTML用の改行処理 (Pixivは改行文字がそのまま改行になる)
                    html_content = html_content.replace('\n', '<br/>\n')
                    
                    # 表紙画像の取得と保存
                    cover_image_path = None
                    cover_url = novel_data.get('coverUrl')
                    if cover_url:
                        novel_img_dir = os.path.join(work_img_dir, "images")
                        os.makedirs(novel_img_dir, exist_ok=True)
                        ext = os.path.splitext(cover_url.split('?')[0])[1] or '.jpg'
                        cover_save_path = os.path.join(novel_img_dir, f"cover_{work_id}{ext}")
                        if not os.path.exists(cover_save_path):
                            client.download_image(cover_url, cover_save_path)
                        cover_image_path = cover_save_path
                    
                    if novel_save_format in ('epub', 'both'):
                        epub_name = f"{work_id}_{safe_title}.epub"
                        epub_path = os.path.join(work_img_dir, epub_name)
                        epub_builder.create_epub(
                            output_path=epub_path,
                            title=title,
                            author=user_name,
                            content_html=html_content,
                            cover_image_path=cover_image_path,
                            embedded_images=epub_embedded_images
                        )
                        log(f"小説のEPUB保存に成功しました: {epub_name}", "DEBUG")
                        
                    if novel_save_format in ('txt', 'both'):
                        txt_name = f"{work_id}_{safe_title}.txt"
                        txt_path = os.path.join(work_img_dir, txt_name)
                        with open(txt_path, 'w', encoding='utf-8-sig') as f:
                            f.write(txt_content)
                        log(f"小説のTXT保存に成功しました: {txt_name}", "DEBUG")
                        
                    # 品質チェックを先に実施し、成功時のみ DB を更新する
                    is_valid, reason = verify_work_integrity(epub_path if novel_save_format in ('epub', 'both') else txt_path)
                    if not is_valid:
                        log(f"小説作品の品質チェック異常を検出しました (ID: {work_id}): {reason}", "WARNING")
                        db.add_failed_job(work_id, user_id, title, "novel", reason)
                        stats["failed_count"] += 1
                    else:
                        db.upsert_work(work_id, user_id, title, page_count, create_date, update_date, content_type='novel')
                        db.remove_failed_job(work_id)
                        if status_type == "new":
                            stats["new_count"] += 1
                        elif status_type == "updated":
                            stats["updated_count"] += 1
                        elif status_type == "restored":
                            stats["restored_count"] += 1
                        elif status_type == "skipped":
                            stats["skipped_count"] += 1
                    log(f"作品DBを更新しました: {title}", "DEBUG")


                elif work.get("type") in (2, "ugoira"):
                    is_ok, reason, saved_file_paths = save_ugoira(
                        client, work_id, title, user_id, user_name,
                        work_img_dir, use_work_folder, safe_title,
                        check_state, log, db, ugoira_save_format
                    )
                    if not is_ok:
                        log(f"うごイラ作品の保存に失敗しました (ID: {work_id}): {reason}", "WARNING")
                        db.add_failed_job(work_id, user_id, title, "ugoira", reason)
                        stats["failed_count"] += 1
                        continue

                    is_valid, reason = verify_work_integrity(
                        work_img_dir if ugoira_save_format == "folder" else saved_file_paths[0],
                        file_paths=saved_file_paths if ugoira_save_format == "folder" else [saved_file_paths[0]]
                    )
                    if not is_valid:
                        log(f"うごイラ作品の品質チェック異常を検出しました (ID: {work_id}): {reason}", "WARNING")
                        db.add_failed_job(work_id, user_id, title, "ugoira", reason)
                        stats["failed_count"] += 1
                    else:
                        db.upsert_work(work_id, user_id, title, page_count, create_date, update_date, content_type='ugoira')
                        db.remove_failed_job(work_id)
                        if status_type == "new":
                            stats["new_count"] += 1
                        elif status_type == "updated":
                            stats["updated_count"] += 1
                        elif status_type == "restored":
                            stats["restored_count"] += 1
                        elif status_type == "skipped":
                            stats["skipped_count"] += 1
                        log(f"作品DBを更新しました: {title}", "DEBUG")


                else:
                    img_urls = client.get_image_urls(work_id)
                    if not img_urls:
                        reason = "画像URLが見つかりませんでした"
                        log(f"作品ID: {work_id} の{reason}。", "WARNING")
                        db.add_failed_job(work_id, user_id, title, "illust", reason)
                        stats["failed_count"] += 1
                        continue
                    
                    saved_file_paths = []
                    for page_idx, img_url in enumerate(img_urls):
                        check_state()
                        ext = os.path.splitext(img_url.split('?')[0])[1]
                        if not ext:
                            ext = '.jpg'

                        if use_work_folder:
                            filename = os.path.basename(img_url.split('?')[0])
                        else:
                            if len(img_urls) == 1:
                                filename = f"{safe_title}{ext}"
                            else:
                                filename = f"{safe_title}_{page_idx + 1}{ext}"

                        save_path = os.path.join(work_img_dir, filename)
                        saved_file_paths.append(save_path)

                        if os.path.exists(save_path):
                            log(f"画像は既に存在するためスキップします: {filename}", "DEBUG")
                        else:
                            client.download_image(img_url, save_path)
                            log(f"画像の保存に成功しました: {filename}", "DEBUG")

                    # 品質チェックを先に実施し、成功時のみ DB を更新する (upsert_work内部でcommitされる)
                    is_valid, reason = verify_work_integrity(work_img_dir, expected_page_count=page_count, file_paths=saved_file_paths)
                    if not is_valid:
                        log(f"イラスト作品の品質チェック異常を検出しました (ID: {work_id}): {reason}", "WARNING")
                        db.add_failed_job(work_id, user_id, title, "illust", reason)
                        stats["failed_count"] += 1
                    else:
                        db.upsert_work(work_id, user_id, title, page_count, create_date, update_date, content_type='illust')
                        db.remove_failed_job(work_id)
                        if status_type == "new":
                            stats["new_count"] += 1
                        elif status_type == "updated":
                            stats["updated_count"] += 1
                        elif status_type == "restored":
                            stats["restored_count"] += 1
                        elif status_type == "skipped":
                            stats["skipped_count"] += 1


            except Exception as e:
                if stop_event and stop_event.is_set():
                    raise
                db.add_failed_job(work_id, user_id, title if 'title' in locals() else str(work_id), 'illust', f"ダウンロードエラー: {str(e)}")
                stats["failed_count"] += 1
                log(f"作品ID: {work_id} の処理中にエラーが発生しました: {e}", "ERROR")
                continue

    # 【フェーズD】ダウンロード完了後のZip化判定と実行
    zip_all = db.get_setting("zip_all_after_download", "0") == "1"
    cursor = db.conn.execute("SELECT name, is_zipped FROM following_users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    # 作者個別設定(author_settings.auto_archive)が設定されていれば、
    # following_users.is_zipped（一覧のアーカイブアイコン）より優先して適用する。
    override_zip = db.get_author_setting(user_id, "auto_archive", None)
    if override_zip in ("1", "0"):
        is_zipped = (override_zip == "1")
    else:
        is_zipped = bool(row['is_zipped']) if row else False

    if zip_all or is_zipped:
        # 今回何もダウンロードしなかった場合(author_dir_nameが未設定)でも、ZIP化設定が
        # 有効なら既存フォルダを対象にZIP化できるよう、ユーザー名を独立して解決する。
        zip_user_name = resolved_user_name or (row['name'] if row and row['name'] else None) or user_id
        zip_author_dir_name = f"{sanitize_filename(zip_user_name)}({user_id})"
        zip_target_dir = os.path.join(base_img_dir, zip_author_dir_name)
        zip_target_path = os.path.join(base_img_dir, f"{zip_author_dir_name}.zip")
        if os.path.exists(zip_target_dir):
            log(f"Zip圧縮（追記）を実行します: {zip_target_path}")
            append_to_zip(zip_target_dir, zip_target_path, log_callback=log)
            _verify_zip_and_notify(db, zip_target_path, zip_author_dir_name, log, alert)

    log(f"--- [結果サマリー] 〇新規: {stats['new_count']}件 | △更新: {stats['updated_count']}件 | ×削除検知: {stats['deleted_count']}件 | ↺復帰: {stats['restored_count']}件 | スキップ: {stats['skipped_count']}件 | エラー: {stats['failed_count']}件 ---")
    return stats

def run_single_work_backup(work_id: str, is_novel: bool, client: PixivClient, db: Database, log_callback=None):
    logger = logging.getLogger(__name__)
    
    def log(msg, level="INFO"):
        if level == "INFO":
            logger.info(msg)
        elif level == "WARNING":
            logger.warning(msg)
        elif level == "ERROR":
            logger.error(msg)
        elif level == "DEBUG":
            logger.debug(msg)
            return
        if log_callback:
            log_callback(msg)
            
    log(f"単一作品ダウンロードを開始します (ID: {work_id}, 小説: {is_novel})")
    
    if is_novel:
        work = client.get_novel_info(work_id)
    else:
        work = client.get_work_info(work_id)
        
    if not work:
        log(f"作品ID: {work_id} の情報が取得できませんでした。", "ERROR")
        return
        
    title = work.get('title', '無題')
    user_name = work.get('user_name', 'Unknown')
    user_id = work.get('user_id', 'Unknown')
    page_count = work.get('page_count', work.get('text_count', 1))
    create_date = work.get('create_date', '')
    update_date = work.get('update_date', '')
    work_type = work.get('type', 'illust')
    
    db_record = db.get_work(work_id)
    if db_record and db_record['update_date'] == update_date and not db_record['is_deleted']:
        log(f"この作品は既に最新の状態で保存されています: {title}", "INFO")
        return
        
    log(f"ダウンロード実行: {title}")

    def _do_download_and_zip():
        # GUI手動実行・スケジューラ・拡張機能連携キューが同じ作者を同時に処理し、
        # ZIPアーカイブへの同時追記で破損させることを防ぐため、この関数全体を
        # 作者(user_id)単位のロックで直列化する(呼び出し元で取得済み)。
        nonlocal user_name

        base_img_dir = db.get_setting("save_path", "Images")
        novel_save_format = db.get_setting("novel_save_format", "epub")
        ugoira_save_format = db.get_setting("ugoira_save_format", "gif")
        use_work_folder = db.get_setting("use_work_folder", "0") == "1"

        safe_title = sanitize_filename(title)
        safe_user_name = sanitize_filename(user_name)
        author_dir_name = f"{safe_user_name}({user_id})"
        work_img_dir = os.path.join(base_img_dir, author_dir_name)
        if use_work_folder:
            work_folder_name = f"{safe_title[:30]}({work_id})"
            work_img_dir = os.path.join(work_img_dir, work_folder_name)
        os.makedirs(work_img_dir, exist_ok=True)

        try:
            if is_novel:
                novel_data = client.get_novel_text(work_id)
                raw_content = novel_data.get('content', '')

                embedded_images = novel_data.get('textEmbeddedImages', {})
                epub_embedded_images = []
                txt_content = raw_content
                html_content = raw_content

                if embedded_images:
                    novel_img_dir = os.path.join(work_img_dir, "images")
                    os.makedirs(novel_img_dir, exist_ok=True)
                    for img_id, img_info in embedded_images.items():
                        original_url = img_info.get('urls', {}).get('original')
                        if original_url:
                            ext = os.path.splitext(original_url.split('?')[0])[1] or '.jpg'
                            img_filename = f"{img_id}{ext}"
                            save_path = os.path.join(novel_img_dir, img_filename)
                            if not os.path.exists(save_path):
                                client.download_image(original_url, save_path)
                            txt_content = txt_content.replace(f"[uploadedimage:{img_id}]", f"[挿絵: images/{img_filename}]")
                            html_content = html_content.replace(f"[uploadedimage:{img_id}]", f'<img src="../Images/{img_id}{ext}" alt="挿絵" />')
                            epub_embedded_images.append({'id': img_id, 'path': save_path, 'ext': ext})

                txt_content = re.sub(r'\[\[rb:(.*?) > (.*?)\]\]', r'\1《\2》', txt_content)
                html_content = re.sub(r'\[\[rb:(.*?) > (.*?)\]\]', r'<ruby>\1<rt>\2</rt></ruby>', html_content)
                txt_content = txt_content.replace('[newpage]', '\n\n---\n\n')
                html_content = html_content.replace('[newpage]', '<hr/>')
                html_content = html_content.replace('\n', '<br/>\n')

                cover_image_path = None
                cover_url = novel_data.get('coverUrl')
                if cover_url:
                    novel_img_dir = os.path.join(work_img_dir, "images")
                    os.makedirs(novel_img_dir, exist_ok=True)
                    ext = os.path.splitext(cover_url.split('?')[0])[1] or '.jpg'
                    cover_save_path = os.path.join(novel_img_dir, f"cover_{work_id}{ext}")
                    if not os.path.exists(cover_save_path):
                        client.download_image(cover_url, cover_save_path)
                    cover_image_path = cover_save_path

                if novel_save_format in ('epub', 'both'):
                    epub_name = f"{work_id}_{safe_title}.epub"
                    epub_path = os.path.join(work_img_dir, epub_name)
                    epub_builder.create_epub(
                        output_path=epub_path,
                        title=title,
                        author=user_name,
                        content_html=html_content,
                        cover_image_path=cover_image_path,
                        embedded_images=epub_embedded_images
                    )
                    log(f"小説のEPUB保存に成功しました: {epub_name}", "DEBUG")

                if novel_save_format in ('txt', 'both'):
                    txt_name = f"{work_id}_{safe_title}.txt"
                    txt_path = os.path.join(work_img_dir, txt_name)
                    with open(txt_path, 'w', encoding='utf-8-sig') as f:
                        f.write(txt_content)
                    log(f"小説のTXT保存に成功しました: {txt_name}", "DEBUG")

                db.upsert_work(work_id, str(user_id), title, page_count, create_date, update_date, content_type='novel')

                is_valid, reason = verify_work_integrity(epub_path if novel_save_format in ('epub', 'both') else txt_path)
                if not is_valid:
                    log(f"品質チェック異常を検出しました (ID: {work_id}): {reason}", "WARNING")
                    db.add_failed_job(work_id, user_id, title, work_type, reason)
                else:
                    db.remove_failed_job(work_id)

            elif work_type in (2, "ugoira"):
                is_ok, reason, saved_file_paths = save_ugoira(
                    client, work_id, title, str(user_id), user_name,
                    work_img_dir, use_work_folder, safe_title,
                    lambda: None, log, db, ugoira_save_format
                )
                if not is_ok:
                    log(f"品質チェック/保存異常を検出しました (ID: {work_id}): {reason}", "WARNING")
                    db.add_failed_job(work_id, str(user_id), title, "ugoira", reason)
                else:
                    is_valid, reason = verify_work_integrity(
                        work_img_dir if ugoira_save_format == "folder" else saved_file_paths[0],
                        file_paths=saved_file_paths if ugoira_save_format == "folder" else [saved_file_paths[0]]
                    )
                    if not is_valid:
                        log(f"品質チェック異常を検出しました (ID: {work_id}): {reason}", "WARNING")
                        db.add_failed_job(work_id, str(user_id), title, "ugoira", reason)
                    else:
                        db.upsert_work(work_id, str(user_id), title, page_count, create_date, update_date, content_type='ugoira')
                        db.remove_failed_job(work_id)

            else:
                img_urls = client.get_image_urls(work_id)
                if not img_urls:
                    log(f"作品ID: {work_id} の画像URLが見つかりませんでした。", "WARNING")
                    return

                saved_file_paths = []
                for page_idx, img_url in enumerate(img_urls):
                    ext = os.path.splitext(img_url.split('?')[0])[1] or '.jpg'
                    if use_work_folder:
                        filename = os.path.basename(img_url.split('?')[0])
                    else:
                        filename = f"{safe_title}{ext}" if len(img_urls) == 1 else f"{safe_title}_{page_idx + 1}{ext}"
                    save_path = os.path.join(work_img_dir, filename)
                    saved_file_paths.append(save_path)

                    if not os.path.exists(save_path):
                        client.download_image(img_url, save_path)
                        log(f"画像の保存に成功しました: {filename}", "DEBUG")

                db.upsert_work(work_id, str(user_id), title, page_count, create_date, update_date, content_type='illust')

                is_valid, reason = verify_work_integrity(work_img_dir, expected_page_count=page_count, file_paths=saved_file_paths)
                if not is_valid:
                    log(f"品質チェック異常を検出しました (ID: {work_id}): {reason}", "WARNING")
                    db.add_failed_job(work_id, user_id, title, work_type, reason)
                else:
                    db.remove_failed_job(work_id)

            log(f"作品の保存が完了しました: {title}")

            # Zip化はループ後に一括で行うため、ここでは何もしない

        except Exception as e:
            db.add_failed_job(work_id, user_id if 'user_id' in locals() else '', title if 'title' in locals() else str(work_id), work_type if 'work_type' in locals() else 'illust', f"ダウンロードエラー: {str(e)}")
            log(f"作品ID: {work_id} の処理中にエラーが発生しました: {e}", "ERROR")

        # === ループ終了後 ===
        # 既存フォルダのZIP化（または今回ダウンロードしたファイルのZIP化）を一括で行う
        # まず、DBにユーザーが存在するか確認。存在しなければ追加。

        # ユーザー名を取得（存在しなければ作成。同時実行時の衝突を避けるためアトミックな操作を使用）
        auto_archive = db.get_setting("auto_archive_new_users", "0") == "1"
        try:
            row, created = db.get_or_create_following_user(user_id, user_name, auto_archive)
        except Exception as e:
            log(f"following_users の登録/取得に失敗しました (ID: {user_id}): {e}", "ERROR")
            row, created = None, False

        override_zip = db.get_author_setting(user_id, "auto_archive", None)
        if override_zip in ("1", "0"):
            is_zipped = (override_zip == "1")
        else:
            is_zipped = bool(row['is_zipped']) if row else auto_archive
        if row:
            if user_name == 'Unknown':
                user_name = row['name']
            if created:
                log(f"新規ユーザーをDBに登録しました: {row['name']} (ZIP化: {auto_archive})", "INFO")

        safe_user_name = sanitize_filename(user_name)
        author_dir_name = f"{safe_user_name}({user_id})"

        zip_all = db.get_setting("zip_all_after_download", "0") == "1"

        if zip_all or is_zipped:
            author_dir = os.path.join(base_img_dir, author_dir_name)
            zip_target_path = os.path.join(base_img_dir, f"{author_dir_name}.zip")
            if os.path.exists(author_dir):
                log(f"Zip圧縮（既存フォルダの結合）を実行します: {zip_target_path}", "INFO")
                append_to_zip(author_dir, zip_target_path, log_callback=log)
                _verify_zip_and_notify(db, zip_target_path, author_dir_name, log)

    with get_user_lock(user_id):
        _do_download_and_zip()

def run_batch_backup(user_ids: list[str], client: PixivClient, db: Database, is_full: bool = False, target_type: str = "both",
                     log_callback=None, progress_callback=None, alert_callback=None,
                     stop_event=None, pause_event=None, batch_progress_callback=None, new_only: bool = False):
    logger = logging.getLogger(__name__)
    
    def log(msg, level="INFO"):
        if level == "INFO":
            logger.info(msg)
        elif level == "ERROR":
            logger.error(msg)
        if log_callback:
            log_callback(msg)
            
    def check_state():
        if stop_event and stop_event.is_set():
            raise StopRequested("一括処理がユーザーによって中止されました。")
        if pause_event and pause_event.is_set():
            log("一括処理を一時停止しています...")
            while pause_event.is_set():
                if stop_event and stop_event.is_set():
                    raise StopRequested("一括処理がユーザーによって中止されました。")
                pause_event.wait(timeout=1.0)
            log("一括処理を再開します。")

    total_users = len(user_ids)
    batch_start_time = time.perf_counter()
    total_stats = {
        "new_count": 0,
        "updated_count": 0,
        "deleted_count": 0,
        "restored_count": 0,
        "skipped_count": 0,
        "failed_count": 0
    }
    for idx, user_id in enumerate(user_ids, 1):
        check_state()
        if batch_progress_callback:
            elapsed = time.perf_counter() - batch_start_time
            batch_progress_callback(idx, total_users, user_id, elapsed)
            
        log(f"--- [{idx}/{total_users}] ユーザーID: {user_id} の処理を開始します ---")
        try:
            u_stats = run_backup(
                user_id=user_id, client=client, db=db, is_full=is_full, target_type=target_type,
                log_callback=log_callback, progress_callback=progress_callback,
                alert_callback=alert_callback, stop_event=stop_event, pause_event=pause_event,
                new_only=new_only
            )
            if u_stats and isinstance(u_stats, dict):
                for k in total_stats:
                    total_stats[k] += u_stats.get(k, 0)
            # 完了したらDBに最終ダウンロード日時を記録
            db.update_following_last_downloaded(user_id)
            
        except Exception as e:
            if stop_event and stop_event.is_set():
                raise
            log(f"ユーザーID: {user_id} の処理中にエラーが発生しました: {e}", "ERROR")
            
        # 次のユーザーへ移行する前に3〜5秒スリープしてサーバー負荷を軽減
        if idx < total_users:
            check_state()
            sleep_time = 3.0
            log(f"サーバー負荷軽減のため {sleep_time} 秒待機します...")
            time.sleep(sleep_time)
            
    log(f"=== [全件一括ダウンロード完了サマリー] 〇新規: {total_stats['new_count']}件 | △更新: {total_stats['updated_count']}件 | ×削除検知: {total_stats['deleted_count']}件 | ↺復帰: {total_stats['restored_count']}件 | スキップ: {total_stats['skipped_count']}件 | エラー: {total_stats['failed_count']}件 ===")
    return total_stats

def export_data(db: Database, log_callback=None):
    logger = logging.getLogger(__name__)
    
    def log(msg, level="INFO"):
        if level == "INFO":
            logger.info(msg)
        elif level == "ERROR":
            logger.error(msg)
            
        if log_callback:
            log_callback(msg)
            
    log("エクスポート処理を開始します。")
    
    timestamp = datetime.now().strftime("%Y%m%d")
    export_dir = f"PixivVault_Backup_{timestamp}"
    zip_name = f"{export_dir}.zip"
    
    try:
        os.makedirs(export_dir, exist_ok=True)
        
        log("メタデータのJSONダンプを作成しています...")
        works_data = []
        cursor = db.conn.execute("SELECT * FROM works")
        for row in cursor.fetchall():
            works_data.append(dict(row))
            
        json_path = os.path.join(export_dir, "metadata.json")
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(works_data, f, ensure_ascii=False, indent=4)
            
        log("データベースファイルをコピーしています...")
        if os.path.exists("pixiv_vault.db"):
            shutil.copy2("pixiv_vault.db", os.path.join(export_dir, "pixiv_vault.db"))
            
        log("画像フォルダをコピーしています（これには時間がかかる場合があります）...")
        if os.path.exists("Images"):
            shutil.copytree("Images", os.path.join(export_dir, "Images"), dirs_exist_ok=True)
            
        log(f"アーカイブ {zip_name} を作成しています...")
        shutil.make_archive(export_dir, 'zip', export_dir)
        
        log(f"エクスポートが完了しました: {zip_name}")
        
    finally:
        if os.path.exists(export_dir):
            shutil.rmtree(export_dir)

def check_work_exists_on_disk(work_id, db, base_img_dir: str, bookmark_base_dir: str = None, dir_cache: dict = None):
    """
    指定された作品IDの実体ファイルまたはフォルダが存在するか確認する。
    存在する場合はTrue、物理削除されている場合はFalseを返す。

    dir_cache に dict を渡すと、ディレクトリごとの os.listdir() 結果をキャッシュする。
    download_bookmarks のようにブックマーク件数分この関数を連続で呼ぶ場合、
    同じ著者ディレクトリを毎回再スキャンする無駄を避けられる。
    """
    import os

    def _listdir(path):
        if dir_cache is None:
            return os.listdir(path) if os.path.exists(path) else []
        cached = dir_cache.get(path)
        if cached is None:
            cached = os.listdir(path) if os.path.exists(path) else []
            dir_cache[path] = cached
        return cached

    work_record = db.get_work(work_id)
    if not work_record:
        return False

    user_id = work_record['user_id']
    content_type = dict(work_record).get('content_type', 'illust')

    use_work_folder = db.get_setting("use_work_folder", "0") == "1"
    novel_save_format = db.get_setting("novel_save_format", "epub")
    ugoira_save_format = db.get_setting("ugoira_save_format", "gif")
    bookmark_download_mode = db.get_setting("bookmark_download_mode", "direct")

    # ブックマークの direct モードの場合、bookmark_base_dir への保存をチェックする
    if bookmark_base_dir and bookmark_download_mode == "direct":
        # download_bookmarks の direct モードは "{author_dir_name}_{work_id}_{元のファイル名}" という
        # 命名規則でハードリンクを作成するため、その work_id トークンの有無で判定する。
        # (タイトルベースの元ファイル名には work_id が含まれないため、拡張子等での判定はできない)
        marker = f"_{work_id}_"
        for f in _listdir(bookmark_base_dir):
            if marker in f:
                return True
        return False

    user_dir = os.path.join(base_img_dir, str(user_id))

    if content_type == 'illust':
        if use_work_folder:
            work_dir = os.path.join(user_dir, str(work_id))
            return os.path.exists(work_dir)
        else:
            for f in _listdir(user_dir):
                if f.startswith(f"{work_id}_"):
                    return True
            return False
    elif content_type == 'novel':
        if use_work_folder:
            work_dir = os.path.join(user_dir, str(work_id))
            return os.path.exists(work_dir)
        else:
            if novel_save_format == "epub":
                return os.path.exists(os.path.join(user_dir, f"{work_id}.epub"))
            else:
                return os.path.exists(os.path.join(user_dir, f"{work_id}.txt"))
    elif content_type == 'ugoira':
        if use_work_folder:
            work_dir = os.path.join(user_dir, str(work_id))
            return os.path.exists(work_dir)
        else:
            work_record_title = dict(work_record).get('title', '無題') if work_record else '無題'
            safe_t = sanitize_filename(work_record_title)
            if ugoira_save_format == "folder":
                return os.path.exists(os.path.join(user_dir, f"{safe_t[:30]}({work_id})"))
            elif ugoira_save_format == "mp4":
                return os.path.exists(os.path.join(user_dir, f"{safe_t}_{work_id}.mp4")) or os.path.exists(os.path.join(user_dir, f"{safe_t}.mp4"))
            else:
                return os.path.exists(os.path.join(user_dir, f"{safe_t}_{work_id}.gif")) or os.path.exists(os.path.join(user_dir, f"{safe_t}.gif"))
    return False
def download_bookmarks(db: Database, client: PixivClient, my_user_id: str, target_type: str = "both", rest_type: str = "show", log_callback=None, progress_callback=None, alert_callback=None, stop_event=None, pause_event=None):
    logger = logging.getLogger(__name__)
    
    def log(msg, level="INFO"):
        if level == "INFO":
            logger.info(msg)
        elif level == "WARNING":
            logger.warning(msg)
        elif level == "ERROR":
            logger.error(msg)
        elif level == "DEBUG":
            logger.debug(msg)
            return
        
        if log_callback:
            log_callback(msg)
            
    def alert(msg):
        logger.warning(msg)
        if alert_callback:
            alert_callback(msg)
        elif log_callback:
            log_callback(f"⚠️ {msg}")

    def check_state():
        if stop_event and stop_event.is_set():
            raise StopRequested("処理がユーザーによって中止されました。")
        if pause_event and pause_event.is_set():
            log("処理を一時停止しています...", "INFO")
            while pause_event.is_set():
                if stop_event and stop_event.is_set():
                    raise StopRequested("処理がユーザーによって中止されました。")
                pause_event.wait(timeout=1.0)
            log("処理を再開します。", "INFO")

    check_state()
    log(f"【フェーズA】Pixivからブックマーク一覧({target_type}:{rest_type})を取得します。")
    current_works = []
    if target_type in ("illust", "both"):
        current_works.extend(client.get_bookmarked_works(my_user_id, rest_type=rest_type, log_callback=log_callback))
    if target_type in ("novel", "both"):
        current_works.extend(client.get_bookmarked_novels(my_user_id, rest_type=rest_type, log_callback=log_callback))
    
    if not current_works:
        log("保存対象のブックマークがありませんでした。")
        return
        
    log("【フェーズB】画像のダウンロードとDBの更新を行います。")
    base_img_dir = db.get_setting("save_path", "Images")
    novel_save_format = db.get_setting("novel_save_format", "epub")
    ugoira_save_format = db.get_setting("ugoira_save_format", "gif")
    use_work_folder = db.get_setting("use_work_folder", "0") == "1"
    enable_hardlink = db.get_setting("enable_bookmark_hardlink", "1") == "1"
    
    bookmark_base_dir = os.path.join(base_img_dir, f"☆ブックマーク({my_user_id})")
    if enable_hardlink:
        os.makedirs(bookmark_base_dir, exist_ok=True)
        
    total = len(current_works)
    start_time = time.perf_counter()
    # ディレクトリごとの os.listdir() 結果をキャッシュし、同一著者の作品を大量に
    # ブックマークしている場合に check_work_exists_on_disk が同じディレクトリを
    # 何度も再スキャンしてしまう無駄を避ける。
    disk_check_cache = {}

    # Needs a small tweak for 'user_id' because bookmark works might have different authors
    is_full = False
    for idx, work in enumerate(current_works, 1):
        check_state()
        if progress_callback:
            elapsed = time.perf_counter() - start_time
            progress_callback(idx, total, elapsed)
            
        work_id = str(work['id'])
        title = work.get('title', '無題')
        user_name = work.get('user_name', 'Unknown')
        user_id = str(work.get('user_id', ''))
        page_count = work.get('page_count', 1)
        create_date = work.get('create_date', '')
        update_date = work.get('update_date', '')
        
        db_record = db.get_work(work_id)
        needs_download = False
        
        bookmark_download_mode = db.get_setting("bookmark_download_mode", "direct")
        exists_on_disk = check_work_exists_on_disk(work_id, db, base_img_dir, bookmark_base_dir, dir_cache=disk_check_cache)
        
        if db_record and not exists_on_disk:
            log(f"[{idx}/{total}] 物理ファイルが見つかりません。再DLのためDBから削除します。 ID: {work_id}")
            db.delete_work(work_id)
            db_record = None

        if is_full:
            log(f"[{idx}/{total}] 完全チェック: ID: {work_id} 「{title}」")
            needs_download = True
        else:
            if not db_record:
                log(f"[{idx}/{total}] 新規作品(または物理削除済)を発見！ ID: {work_id} 「{title}」")
                needs_download = True
            elif db_record['update_date'] != update_date:
                log(f"[{idx}/{total}] 更新された作品を発見！ ID: {work_id} 「{title}」")
                needs_download = True
            elif db_record['is_deleted']:
                log(f"[{idx}/{total}] 削除状態から復帰した作品を発見！ ID: {work_id} 「{title}」")
                needs_download = True
            else:
                pass
            
        try:
            work_type = work.get('type', 'illust')
            safe_title = sanitize_filename(title)
            safe_user_name = sanitize_filename(user_name)
            author_dir_name = f"{safe_user_name}({user_id})"
            work_img_dir = os.path.join(base_img_dir, author_dir_name)
            if use_work_folder:
                work_folder_name = f"{safe_title[:30]}({work_id})"
                work_img_dir = os.path.join(work_img_dir, work_folder_name)
                
            if needs_download:
                os.makedirs(work_img_dir, exist_ok=True)
                
                if work_type == 'novel':
                    novel_data = client.get_novel_text(work_id)
                    raw_content = novel_data.get('content', '')
                    
                    # 挿絵の処理とダウンロード
                    embedded_images = novel_data.get('textEmbeddedImages', {})
                    epub_embedded_images = []
                    txt_content = raw_content
                    html_content = raw_content
                    
                    if embedded_images:
                        novel_img_dir = os.path.join(work_img_dir, "images")
                        os.makedirs(novel_img_dir, exist_ok=True)
                        for img_id, img_info in embedded_images.items():
                            original_url = img_info.get('urls', {}).get('original')
                            if original_url:
                                ext = os.path.splitext(original_url.split('?')[0])[1] or '.jpg'
                                img_filename = f"{img_id}{ext}"
                                save_path = os.path.join(novel_img_dir, img_filename)
                                if not os.path.exists(save_path):
                                    client.download_image(original_url, save_path)
                                
                                # TXT用
                                txt_content = txt_content.replace(f"[uploadedimage:{img_id}]", f"[挿絵: images/{img_filename}]")
                                # HTML (EPUB) 用
                                html_content = html_content.replace(f"[uploadedimage:{img_id}]", f'<img src="../Images/{img_id}{ext}" alt="挿絵" />')
                                epub_embedded_images.append({'id': img_id, 'path': save_path, 'ext': ext})
                    
                    # ルビの変換
                    # TXT用: [[rb:漢字 > かんじ]] → 漢字《かんじ》
                    txt_content = re.sub(r'\[\[rb:(.*?) > (.*?)\]\]', r'\1《\2》', txt_content)
                    # HTML用: [[rb:漢字 > かんじ]] → <ruby>漢字<rt>かんじ</rt></ruby>
                    html_content = re.sub(r'\[\[rb:(.*?) > (.*?)\]\]', r'<ruby>\1<rt>\2</rt></ruby>', html_content)
                    
                    # Pixiv独自の改ページタグ [newpage] を処理
                    txt_content = txt_content.replace('[newpage]', '\n\n---\n\n')
                    html_content = html_content.replace('[newpage]', '<hr/>')
                    
                    # HTML用の改行処理 (Pixivは改行文字がそのまま改行になる)
                    html_content = html_content.replace('\n', '<br/>\n')
                    
                    # 表紙画像の取得と保存
                    cover_image_path = None
                    cover_url = novel_data.get('coverUrl')
                    if cover_url:
                        novel_img_dir = os.path.join(work_img_dir, "images")
                        os.makedirs(novel_img_dir, exist_ok=True)
                        ext = os.path.splitext(cover_url.split('?')[0])[1] or '.jpg'
                        cover_save_path = os.path.join(novel_img_dir, f"cover_{work_id}{ext}")
                        if not os.path.exists(cover_save_path):
                            client.download_image(cover_url, cover_save_path)
                        cover_image_path = cover_save_path
                    
                    if novel_save_format in ('epub', 'both'):
                        epub_name = f"{work_id}_{safe_title}.epub"
                        epub_path = os.path.join(work_img_dir, epub_name)
                        epub_builder.create_epub(
                            output_path=epub_path,
                            title=title,
                            author=user_name,
                            content_html=html_content,
                            cover_image_path=cover_image_path,
                            embedded_images=epub_embedded_images
                        )
                        log(f"小説のEPUB保存に成功しました: {epub_name}", "DEBUG")
                        
                    if novel_save_format in ('txt', 'both'):
                        txt_name = f"{work_id}_{safe_title}.txt"
                        txt_path = os.path.join(work_img_dir, txt_name)
                        with open(txt_path, 'w', encoding='utf-8-sig') as f:
                            f.write(txt_content)
                        log(f"小説のTXT保存に成功しました: {txt_name}", "DEBUG")
                        
                    db.upsert_work(work_id, user_id, title, page_count, create_date, update_date, content_type='novel')
                    log(f"作品DBを更新しました: {title}", "DEBUG")

                elif work.get("type") in (2, "ugoira"):
                    is_ok, reason, saved_file_paths = save_ugoira(
                        client, work_id, title, user_id, user_name,
                        work_img_dir, use_work_folder, safe_title,
                        check_state, log, db, ugoira_save_format
                    )
                    if not is_ok:
                        log(f"うごイラ作品の保存に失敗しました (ID: {work_id}): {reason}", "WARNING")
                        continue

                    is_valid, reason = verify_work_integrity(
                        work_img_dir if ugoira_save_format == "folder" else saved_file_paths[0],
                        file_paths=saved_file_paths if ugoira_save_format == "folder" else [saved_file_paths[0]]
                    )
                    if not is_valid:
                        log(f"うごイラ作品の品質チェック異常を検出しました (ID: {work_id}): {reason}", "WARNING")
                    else:
                        db.upsert_work(work_id, user_id, title, page_count, create_date, update_date, content_type='ugoira')
                        log(f"作品DBを更新しました: {title}", "DEBUG")

                else:
                    img_urls = client.get_image_urls(work_id)
                    if not img_urls:
                        log(f"作品ID: {work_id} の画像URLが見つかりませんでした。", "WARNING")
                        continue
                    
                    for page_idx, img_url in enumerate(img_urls):
                        check_state()
                        ext = os.path.splitext(img_url.split('?')[0])[1]
                        if not ext:
                            ext = '.jpg'
                            
                        if use_work_folder:
                            filename = os.path.basename(img_url.split('?')[0])
                        else:
                            if len(img_urls) == 1:
                                filename = f"{safe_title}{ext}"
                            else:
                                filename = f"{safe_title}_{page_idx + 1}{ext}"
                            
                        save_path = os.path.join(work_img_dir, filename)
                        
                        if os.path.exists(save_path):
                            log(f"画像は既に存在するためスキップします: {filename}", "DEBUG")
                        else:
                            client.download_image(img_url, save_path)
                            log(f"画像の保存に成功しました: {filename}", "DEBUG")
                    
                    # 全ページの確認・ダウンロードが成功したらDBを更新 (upsert_work内部でcommitされる)
                    db.upsert_work(work_id, user_id, title, page_count, create_date, update_date, content_type='illust')
            # ハードリンクまたはコピー作成 (needs_downloadに関係なく実行してブックマークフォルダとの同期を保証)
            if bookmark_download_mode in ("link", "direct"):
                try:
                    if bookmark_download_mode == "link":
                        bookmark_author_dir = os.path.join(bookmark_base_dir, author_dir_name)
                        bookmark_work_dir = os.path.join(bookmark_author_dir, work_folder_name) if use_work_folder else bookmark_author_dir
                        os.makedirs(bookmark_work_dir, exist_ok=True)
                    else:
                        bookmark_work_dir = bookmark_base_dir
                        os.makedirs(bookmark_work_dir, exist_ok=True)
                    
                    if os.path.exists(work_img_dir):
                        for root, _, files in os.walk(work_img_dir):
                            for file in files:
                                if not use_work_folder and work_type == 'illust':
                                    if not file.startswith(safe_title):
                                        continue
                                
                                src_file = os.path.join(root, file)
                                if bookmark_download_mode == "direct":
                                    # work_id をファイル名に埋め込んでおくことで、check_work_exists_on_disk
                                    # 側が確実にこの作品のファイルだと判別できるようにする
                                    # (タイトルベースの元ファイル名には work_id が含まれないため)
                                    dst_file = os.path.join(bookmark_work_dir, f"{author_dir_name}_{work_id}_{file}")
                                else:
                                    rel_path = os.path.relpath(src_file, work_img_dir)
                                    dst_file = os.path.join(bookmark_work_dir, rel_path)
                                
                                if not os.path.exists(dst_file):
                                    dst_dir = os.path.dirname(dst_file)
                                    os.makedirs(dst_dir, exist_ok=True)
                                    os.link(src_file, dst_file)
                except Exception as e:
                    log(f"ハードリンク作成失敗 (ID: {work_id}): {e}", "WARNING")

        except Exception as e:
            if stop_event and stop_event.is_set():
                raise
            log(f"作品ID: {work_id} の処理中にエラーが発生しました: {e}", "ERROR")
            continue


    log("ブックマークの一括ダウンロード処理が完了しました。", "INFO")


def get_unfollowed_bookmark_authors(db, client, my_user_id, target_type="both", rest_type="show", log_callback=None):
    import logging
    logger = logging.getLogger(__name__)
    def log(msg, level="INFO"):
        if level == "ERROR": logger.error(msg)
        elif level == "DEBUG": logger.debug(msg)
        else:
            if log_callback: log_callback(msg)
            
    log("未フォロー作者を抽出するためにブックマーク一覧を取得します...")
    
    current_works = []
    if target_type in ("illust", "both"):
        current_works.extend(client.get_bookmarked_works(my_user_id, rest_type=rest_type, log_callback=log_callback))
    if target_type in ("novel", "both"):
        current_works.extend(client.get_bookmarked_novels(my_user_id, rest_type=rest_type, log_callback=log_callback))
        
    following_users = db.get_following_users()
    following_ids = {str(u['user_id']) for u in following_users}
    
    unfollowed_authors = {}
    for work in current_works:
        work_dict = dict(work)
        user_id = str(work_dict.get('user_id', ''))
        if not user_id or user_id in following_ids:
            continue
            
        if user_id not in unfollowed_authors:
            unfollowed_authors[user_id] = {
                'user_id': user_id,
                'user_name': work_dict.get('user_name', 'Unknown'),
                'work_id': work_dict.get('id', ''),
                'work_title': work_dict.get('title', '無題'),
                'thumb_url': work_dict.get('thumb_url', ''),
                'profile_img_url': work_dict.get('profile_img_url', '')
            }
            
    log(f"未フォローの作者を {len(unfollowed_authors)} 人抽出しました。")
    return list(unfollowed_authors.values())
