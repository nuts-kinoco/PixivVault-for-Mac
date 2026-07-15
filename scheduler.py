import time
import threading
from datetime import datetime, timedelta
from notifications import send_notification
from pixiv_client import PixivClient
from database import Database
from core import run_batch_backup

class Scheduler:
    def __init__(self, db: Database, log_callback=None):
        self.db = db
        self.log_callback = log_callback
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def notify(self, title, body):
        if self.db.get_setting("enable_notifications", "1") == "1":
            try:
                send_notification(title, body)
            except Exception as e:
                if self.log_callback:
                    self.log_callback(f"通知エラー: {e}")

    def _loop(self):
        last_check_time = datetime.now()
        while self.running:
            time.sleep(60) # 1分ごとに設定をチェック
            
            interval_str = self.db.get_setting("auto_check_interval_hours", "24")
            try:
                interval = int(interval_str)
            except ValueError:
                interval = 24
            
            if interval <= 0:
                continue

            if datetime.now() - last_check_time > timedelta(hours=interval):
                self._run_check()
                last_check_time = datetime.now()

    def _run_check(self):
        favs = self.db.get_favorite_users()
        if not favs:
            return

        user_ids = [u['user_id'] for u in favs]
        
        if self.log_callback:
            self.log_callback(f"★お気に入り定期チェックを開始します ({len(user_ids)}人)★")

        try:
            client = PixivClient()
            check_start_time = datetime.now().isoformat()
            
            # ダミーのイベント
            stop_event = threading.Event()
            pause_event = threading.Event()

            # 一括バックアップ実行
            run_batch_backup(
                user_ids=user_ids, 
                client=client, 
                db=self.db, 
                is_full=False, 
                target_type="both",
                log_callback=self.log_callback, 
                progress_callback=None, 
                alert_callback=self.log_callback,
                stop_event=stop_event, 
                pause_event=pause_event, 
                batch_progress_callback=None
            )

            # 更新された作品数をカウント
            cursor = self.db.conn.execute(
                "SELECT COUNT(*) as c FROM works WHERE last_backup >= ?", 
                (check_start_time,)
            )
            row = cursor.fetchone()
            downloaded_count = row['c'] if row else 0

            if downloaded_count > 0:
                self.notify(
                    "PixivVault 定期チェック", 
                    f"お気に入りの作者から {downloaded_count} 件の作品を保存しました！"
                )
            
            if self.log_callback:
                self.log_callback(f"★定期チェック完了: {downloaded_count}件保存★")

        except Exception as e:
            if self.log_callback:
                self.log_callback(f"定期チェック中にエラーが発生: {e}")
