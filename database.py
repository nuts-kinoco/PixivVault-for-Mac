import sqlite3
from datetime import datetime

class Database:
    def __init__(self, db_path="pixiv_vault.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_table()

    def _create_table(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS works (
                    work_id TEXT PRIMARY KEY,
                    title TEXT,
                    page_count INTEGER,
                    create_date TEXT,
                    update_date TEXT,
                    last_backup TEXT,
                    is_deleted BOOLEAN DEFAULT 0
                )
            """)
            try:
                self.conn.execute("ALTER TABLE works ADD COLUMN user_id TEXT")
            except sqlite3.OperationalError:
                pass
            
            try:
                self.conn.execute("ALTER TABLE works ADD COLUMN content_type TEXT DEFAULT 'illust'")
            except sqlite3.OperationalError:
                pass

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS following_users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    account TEXT,
                    profile_img TEXT,
                    last_downloaded TEXT,
                    is_zipped BOOLEAN DEFAULT 0
                )
            """)
            try:
                self.conn.execute("ALTER TABLE following_users ADD COLUMN is_zipped BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            
            try:
                self.conn.execute("ALTER TABLE following_users ADD COLUMN is_favorite BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            try:
                self.conn.execute("ALTER TABLE following_users ADD COLUMN follow_order INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS author_settings (
                    user_id TEXT,
                    key TEXT,
                    value TEXT,
                    PRIMARY KEY (user_id, key)
                )
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS failed_queue (
                    work_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    title TEXT,
                    work_type TEXT,
                    error_reason TEXT,
                    failed_at TEXT,
                    retry_count INTEGER DEFAULT 0
                )
            """)

    def get_user_work_ids(self, user_id, content_types=None):
        """指定したユーザーの作品IDを取得します。

        content_types にリストを渡すと、その content_type（'illust'/'novel'）に
        限定して取得します。省略時は従来通り全タイプを対象にします。
        """
        if content_types:
            placeholders = ",".join("?" for _ in content_types)
            cursor = self.conn.execute(
                f"SELECT work_id FROM works WHERE CAST(user_id AS TEXT) = ? AND content_type IN ({placeholders})",
                (str(user_id), *content_types)
            )
        else:
            cursor = self.conn.execute("SELECT work_id FROM works WHERE CAST(user_id AS TEXT) = ?", (str(user_id),))
        return {row['work_id'] for row in cursor.fetchall()}

    def get_work(self, work_id):
        """指定した作品IDのレコードを取得します"""
        cursor = self.conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,))
        return cursor.fetchone()

    def mark_as_deleted(self, work_id):
        """作品がPixivから削除されたことを記録します"""
        with self.conn:
            self.conn.execute("UPDATE works SET is_deleted = 1 WHERE work_id = ?", (work_id,))

    def delete_work(self, work_id):
        """作品IDのレコードをDBから完全に削除します（物理削除時の再DLを促すため）"""
        with self.conn:
            self.conn.execute("DELETE FROM works WHERE work_id = ?", (work_id,))

    def upsert_work(self, work_id, user_id, title, page_count, create_date, update_date, content_type='illust'):
        """作品情報を新規登録、または更新します"""
        now = datetime.now().isoformat()
        w_id = str(work_id) if work_id is not None else None
        u_id = str(user_id) if user_id is not None else None
        with self.conn:
            self.conn.execute("""
                INSERT INTO works (work_id, user_id, title, page_count, create_date, update_date, last_backup, is_deleted, content_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(work_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    title = excluded.title,
                    page_count = excluded.page_count,
                    create_date = excluded.create_date,
                    update_date = excluded.update_date,
                    last_backup = excluded.last_backup,
                    is_deleted = 0,
                    content_type = excluded.content_type
            """, (w_id, u_id, title, page_count, create_date, update_date, now, content_type))

    def save_following_users(self, users_list):
        """フォローしているユーザー一覧をデータベースに保存/更新します

        users_list の並び順は Pixiv 側のフォロー中一覧の並び（フォロー登録順、新しい順）
        をそのまま反映しているため、そのインデックスを follow_order として保存し、
        「フォロー登録順」ソートに利用します。
        """
        with self.conn:
            for order, user in enumerate(users_list):
                self.conn.execute("""
                    INSERT INTO following_users (user_id, name, account, profile_img, last_downloaded, follow_order)
                    VALUES (?, ?, ?, ?, (SELECT last_downloaded FROM following_users WHERE user_id = ?), ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        name = excluded.name,
                        account = excluded.account,
                        profile_img = excluded.profile_img,
                        follow_order = excluded.follow_order
                """, (user['user_id'], user['name'], user['account'], user['profile_img'], user['user_id'], order))

    def get_following_users(self, sort_by="follow_order", sort_order="asc"):
        """保存されているフォローユーザー一覧を取得します

        sort_by: "name"（名前） または "follow_order"（フォロー登録順、新しい順が0）
        sort_order: "asc" または "desc"
        """
        column = "follow_order" if sort_by != "name" else "name COLLATE NOCASE"
        direction = "DESC" if str(sort_order).lower() == "desc" else "ASC"
        cursor = self.conn.execute(f"SELECT * FROM following_users ORDER BY {column} {direction}")
        return [dict(row) for row in cursor.fetchall()]

    def clear_following_users(self):
        """保存されているフォローユーザー一覧をすべてクリア（削除）します"""
        with self.conn:
            self.conn.execute("DELETE FROM following_users")

    def update_following_last_downloaded(self, user_id):
        """特定のフォローユーザーの最終ダウンロード日時を更新します"""
        now = datetime.now().isoformat()
        with self.conn:
            self.conn.execute("""
                UPDATE following_users 
                SET last_downloaded = ? 
                WHERE user_id = ?
            """, (now, user_id))

    def get_setting(self, key, default=None):
        """設定値を取得します"""
        cursor = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row['value'] if row else default

    def set_setting(self, key, value):
        """設定値を保存/更新します"""
        with self.conn:
            self.conn.execute("""
                INSERT INTO settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, str(value)))

    def set_zipped(self, user_id, is_zipped):
        """特定のユーザーのZip圧縮対象状態を更新します"""
        with self.conn:
            self.conn.execute("UPDATE following_users SET is_zipped = ? WHERE user_id = ?", (int(is_zipped), user_id))

    def set_favorite(self, user_id, is_favorite):
        """特定のユーザーをお気に入り状態を更新します"""
        with self.conn:
            self.conn.execute("UPDATE following_users SET is_favorite = ? WHERE user_id = ?", (int(is_favorite), user_id))

    def get_favorite_users(self):
        """お気に入りに設定されているユーザー一覧を取得します"""
        cursor = self.conn.execute("SELECT * FROM following_users WHERE is_favorite = 1")
        return [dict(row) for row in cursor.fetchall()]

    def get_or_create_following_user(self, user_id, name, auto_archive=False):
        """following_users にレコードがなければ作成し、常に最新の行を返します。

        SELECT してから INSERT する方式は複数スレッドから同時に呼ばれると
        (拡張機能経由とGUI経由の並行実行など) IntegrityError で衝突するため、
        ON CONFLICT DO NOTHING による単一のアトミックな INSERT に統一しています。
        戻り値は (row, created) のタプルで、created は今回新規作成した場合 True。
        """
        with self.conn:
            cursor = self.conn.execute("""
                INSERT INTO following_users (user_id, name, is_zipped)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
            """, (str(user_id), name, 1 if auto_archive else 0))
            created = cursor.rowcount > 0
            row = self.conn.execute(
                "SELECT name, is_zipped FROM following_users WHERE user_id = ?", (str(user_id),)
            ).fetchone()
            return row, created

    def add_failed_job(self, work_id, user_id, title, work_type, error_reason):
        """失敗したダウンロードまたは品質チェック異常を失敗キューに追加/更新します"""
        now = datetime.now().isoformat()
        w_id = str(work_id) if work_id is not None else ""
        u_id = str(user_id) if user_id is not None else ""
        with self.conn:
            self.conn.execute("""
                INSERT INTO failed_queue (work_id, user_id, title, work_type, error_reason, failed_at, retry_count)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(work_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    title = excluded.title,
                    work_type = excluded.work_type,
                    error_reason = excluded.error_reason,
                    failed_at = excluded.failed_at,
                    retry_count = failed_queue.retry_count + 1
            """, (w_id, u_id, title or "", work_type or "illust", error_reason or "不明なエラー", now))

    def remove_failed_job(self, work_id):
        """指定した作品IDを失敗キューから削除します（正常保存完了時などに使用）"""
        with self.conn:
            self.conn.execute("DELETE FROM failed_queue WHERE work_id = ?", (str(work_id),))

    def get_failed_jobs(self):
        """失敗キューの全レコードを新しい順に取得します"""
        cursor = self.conn.execute("SELECT * FROM failed_queue ORDER BY failed_at DESC")
        return [dict(row) for row in cursor.fetchall()]

    def clear_failed_jobs(self):
        """失敗キューをすべてクリアします"""
        with self.conn:
            self.conn.execute("DELETE FROM failed_queue")

    # --- 作者個別設定オーバーライド ---
    def get_author_setting(self, user_id, key, default=None):
        cursor = self.conn.execute("SELECT value FROM author_settings WHERE user_id = ? AND key = ?", (str(user_id), str(key)))
        row = cursor.fetchone()
        return row['value'] if row else default

    def set_author_setting(self, user_id, key, value):
        with self.conn:
            self.conn.execute("""
                INSERT INTO author_settings (user_id, key, value)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """, (str(user_id), str(key), str(value)))

    def delete_author_setting(self, user_id, key):
        with self.conn:
            self.conn.execute("DELETE FROM author_settings WHERE user_id = ? AND key = ?", (str(user_id), str(key)))

    def get_all_author_settings(self, user_id):
        cursor = self.conn.execute("SELECT key, value FROM author_settings WHERE user_id = ?", (str(user_id),))
        return {row['key']: row['value'] for row in cursor.fetchall()}
