import json
import threading
import queue
import logging
import time
import uuid
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from notifications import send_notification

from database import Database
from pixiv_client import PixivClient
from core import run_backup, run_single_work_backup, run_batch_backup, download_bookmarks

logger = logging.getLogger(__name__)

# 拡張機能のService WorkerからのリクエストはOrigin: chrome-extension://<id>になる。
# 一方、content.js側はService Workerが応答しない場合に直接fetchするフォールバックを持っており、
# その場合ブラウザはOriginをページ自身のもの(https://www.pixiv.net等)として送信する。
# Webページから直接127.0.0.1に叩かれるドライブバイ攻撃を防ぎつつ、このフォールバックも
# 通せるよう、拡張機能自身とpixiv.net(のサブドメイン)からのOriginのみ許可する。
ALLOWED_ORIGIN_PREFIX = 'chrome-extension://'

# iOS版からのリクエストは Origin 許可に加えて、この HTTP ヘッダ (POST/JSON時) または
# クエリパラメータ (SSE等ヘッダを付けられないGET時) でトークンの一致を要求する。
# トークンは PixivVaultServer.get_web_token() が初回アクセス時に自動生成しDBへ保存する。
WEB_TOKEN_HEADER = 'X-PixivVault-Token'

# 手入力しやすいよう、視認性の低い文字(0/O/1/I/L)を除いた英数字にした（LAN内限定・
# 家庭用ルーターのNAT背後という前提での利便性優先のトレードオフ）。8桁でも
# 31^8 ≒ 8500億通りあり、HTTPリクエスト経由の総当たりは現実的な時間では終わらない。
WEB_TOKEN_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
WEB_TOKEN_LENGTH = 8


def generate_web_token():
    return ''.join(secrets.choice(WEB_TOKEN_ALPHABET) for _ in range(WEB_TOKEN_LENGTH))


class PixivVaultRequestHandler(BaseHTTPRequestHandler):
    def _is_loopback_client(self):
        """接続元がこのマシン自身かどうか。Originヘッダはブラウザが強制する値だが、iOSアプリ含む
        任意のTCPクライアントは偽装できるため、サーバーがLAN(0.0.0.0)へバインドされた
        2026-07-22以降、拡張機能専用エンドポイント(ノートークン)の実効的な境界はこちらになる。"""
        return self.client_address[0] in ('127.0.0.1', '::1')

    def _is_allowed_extension_origin(self, origin):
        if not origin:
            return False
        if not self._is_loopback_client():
            return False
        if origin.startswith(ALLOWED_ORIGIN_PREFIX):
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        host = parsed.hostname or ''
        return parsed.scheme == 'https' and (host == 'pixiv.net' or host.endswith('.pixiv.net'))

    def _is_allowed_web_origin(self, origin):
        """iOS版のURLSessionはブラウザと異なりOriginヘッダを送らない。この経路は現在iOS版
        専用（natsukino.com(PixivVault Web)向けのOrigin許可リストは、Web版を技術検証で
        終了させた際に撤去した）。Originが送られてきた時点でブラウザ等の別クライアントと
        判断し拒否する。"""
        return not origin

    def _is_allowed_origin(self, origin):
        """CORSヘッダのエコー可否判定用。拡張機能origin・iOS版origin いずれかでtrue。"""
        return self._is_allowed_extension_origin(origin) or self._is_allowed_web_origin(origin)

    def _send_cors_headers(self, origin=None):
        if origin and self._is_allowed_origin(origin):
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header(
            'Access-Control-Allow-Headers',
            f'Content-Type, Access-Control-Request-Private-Network, {WEB_TOKEN_HEADER}'
        )
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def do_OPTIONS(self):
        origin = self.headers.get('Origin', '')
        self.send_response(200)
        self._send_cors_headers(origin)
        self.end_headers()

    def _check_origin(self):
        """許可されていない Origin からのリクエストを拒否する。True を返したら処理続行可。
        既存の拡張機能連携専用エンドポイント (/api/work, /api/user/*/status, /api/cookie/sync) は
        この関数で拡張機能origin限定のまま維持する（iOS版からは呼べない）。"""
        origin = self.headers.get('Origin', '')
        if not self._is_allowed_extension_origin(origin):
            self._send_response(403, {"status": "error", "message": "Forbidden"})
            return False
        return True

    def _check_web_token(self):
        """iOS版エンドポイントの共有トークン認証。True を返したら処理続行可。"""
        token = self.headers.get(WEB_TOKEN_HEADER, '')
        if not token or token != self.server.get_web_token():
            self._send_response(401, {"status": "error", "message": "Invalid or missing token"})
            return False
        return True

    def _check_web_bridge_enabled(self):
        """GUIのトグルでiOS版連携が一時的に無効化されていないか確認する。
        拡張機能連携（_is_allowed_extension_origin側）には一切影響しない、web-origin系
        エンドポイント専用のガード。テスト時に「アプリ未接続」状態を意図的に作れるように、
        サーバー自体を落とさずに接続受け付けだけを止められるようにするためのもの。"""
        if not self.server.is_web_bridge_enabled():
            self._send_response(503, {"status": "error", "message": "PC要塞側でこの連携が一時的に無効になっています"})
            return False
        return True

    def do_GET(self):
        # /ping と /progress/<jobId> は拡張機能・iOS版双方からアクセスされうるため
        # 従来の拡張機能限定 _check_origin() より先に、それぞれ専用の判定へ振り分ける。
        if self.path == '/ping':
            self._handle_ping()
            return
        if self.path.startswith('/progress/'):
            self._handle_web_progress()
            return
        if not self._check_origin():
            return
        if self.path.startswith('/api/work/'):
            work_id = self.path.split('/')[-1].split('?')[0]
            work = self.server.db.get_work(work_id)
            if work:
                self._send_response(200, {
                    "downloaded": True,
                    "update_date": work["update_date"],
                    "is_deleted": bool(work["is_deleted"]),
                    "is_latest": True
                })
            else:
                self._send_response(200, {"downloaded": False})
        elif self.path.startswith('/api/user/') and self.path.endswith('/status'):
            parts = self.path.split('/')
            if len(parts) >= 4:
                user_id = parts[3]
                # Pixiv APIではなくDB内の保存数で返す（余分なAPIリクエストを防ぐ）
                db_work_ids = self.server.db.get_user_work_ids(user_id)
                downloaded = len(db_work_ids)
                # DBに保存済みの件数を total として返す（フル表示）
                self._send_response(200, {
                    "downloaded": downloaded,
                    "total": downloaded  # 差分表示は extension 側で別途実装予定
                })
            else:
                self._send_response(400, {"status": "error", "message": "Invalid path"})
        else:
            self._send_response(404, {"status": "error", "message": "Not found"})

    def do_POST(self):
        # /download は拡張機能(既存プロトコル)とiOS版(新プロトコル) の両方が叩くため、
        # _check_origin() (拡張機能限定) より先に Origin 種別で振り分ける。
        if self.path == '/download':
            self._handle_download()
            return
        # /bookmark は iOS版専用（拡張機能は使わない）ため、
        # 最初からトークン認証のみで判定する。
        if self.path == '/bookmark':
            self._handle_bookmark()
            return
        if not self._check_origin():
            return
        if self.path == '/api/cookie/sync':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                cookies_list = data.get('cookies', [])
                if not isinstance(cookies_list, list) or len(cookies_list) == 0:
                    self._send_response(400, {"status": "error", "message": "No cookies provided"})
                else:
                    success, msg = save_cookies_from_extension(cookies_list)
                    if success:
                        self._send_response(200, {"status": "ok", "message": msg})
                    else:
                        self._send_response(400, {"status": "error", "message": msg})
            except Exception as e:
                logger.error(f"Cookie sync error: {e}")
                self._send_response(500, {"status": "error", "message": str(e)})
        else:
            self._send_response(404, {"status": "error", "message": "Not found"})

    def _handle_ping(self):
        """接続確認。接続インジケータの点灯判定に使う。トークンは不要（到達確認のみ）。"""
        origin = self.headers.get('Origin', '')
        if not self._is_allowed_origin(origin):
            self._send_response(403, {"status": "error", "message": "Forbidden"})
            return
        # 拡張機能origin一致は「アプリ接続」とはみなさない（ブラウザ拡張とiOSの
        # 接続インジケータを混同しないため、web-origin側にマッチした場合のみ記録する）。
        if self._is_allowed_web_origin(origin):
            if not self._check_web_bridge_enabled():
                return
            self.server.mark_app_contact()
        self._send_response(200, {"status": "ok"})

    def _handle_download(self):
        origin = self.headers.get('Origin', '')
        if self._is_allowed_extension_origin(origin):
            self._handle_legacy_download()
        elif self._is_allowed_web_origin(origin):
            self._handle_web_download()
        else:
            self._send_response(403, {"status": "error", "message": "Forbidden"})

    def _handle_legacy_download(self):
        """既存の拡張機能連携プロトコル。挙動は変更していない。"""
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)

        try:
            data = json.loads(post_data.decode('utf-8'))
            req_type = data.get('type')

            if req_type == 'user':
                user_id = data.get('user_id')
                new_only = data.get('new_only', False)
                if user_id:
                    if self.server.enqueue(f"user_{user_id}", {'type': 'user', 'user_id': user_id, 'new_only': new_only}):
                        self._send_response(200, {"status": "ok", "message": f"Queued user {user_id} backup"})
                    else:
                        self._send_response(200, {"status": "ok", "message": f"User {user_id} backup already queued"})
                else:
                    self._send_response(400, {"status": "error", "message": "Missing user_id"})
            elif req_type == 'work':
                work_id = data.get('work_id')
                is_novel = data.get('is_novel', False)
                if work_id:
                    if self.server.enqueue(f"work_{work_id}", {'type': 'work', 'work_id': work_id, 'is_novel': is_novel}):
                        self._send_response(200, {"status": "ok", "message": f"Queued work {work_id} backup"})
                    else:
                        self._send_response(200, {"status": "ok", "message": f"Work {work_id} backup already queued"})
                else:
                    self._send_response(400, {"status": "error", "message": "Missing work_id"})
            else:
                self._send_response(400, {"status": "error", "message": "Invalid type"})

        except json.JSONDecodeError:
            self._send_response(400, {"status": "error", "message": "Invalid JSON"})

    def _handle_web_download(self):
        """iOS版 (HostSource.enqueueDownload) 用プロトコル。DownloadCommand 形式の JSON を受け取り、
        既存の core.py バックアップ機構にマッピングしてジョブとしてキューへ積む。トークン必須。"""
        if not self._check_web_bridge_enabled():
            return
        if not self._check_web_token():
            return
        self.server.mark_app_contact()

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        try:
            data = json.loads(post_data.decode('utf-8'))
        except json.JSONDecodeError:
            self._send_response(400, {"status": "error", "message": "Invalid JSON"})
            return

        target = data.get('target') or {}
        kind = target.get('kind')
        target_id = target.get('id')

        if kind not in ('work', 'author', 'bookmarks', 'following'):
            self._send_response(400, {"status": "error", "message": "Invalid target.kind"})
            return
        if kind in ('work', 'author') and not target_id:
            self._send_response(400, {"status": "error", "message": "Missing target.id"})
            return

        # リクエスト単位のダウンロード形式/保存先上書き（P-4）。DownloadCommand.options に
        # 積まれた値のみ反映し、無指定のキーは従来通りグローバル/作者別DB設定にフォールバックする
        # (core.py の各バックアップ関数側で options.get(key) or db.get_setting(...) の形で処理)。
        raw_options = data.get('options') or {}
        options = {}
        if raw_options.get('savePathOverride'):
            options['save_path'] = raw_options['savePathOverride']
        if raw_options.get('zipPolicy') in ('zip', 'individual'):
            options['zip_policy'] = raw_options['zipPolicy']
        if raw_options.get('ugoiraFormat') in ('gif', 'mp4', 'folder'):
            options['ugoira_format'] = raw_options['ugoiraFormat']
        if raw_options.get('novelFormat') in ('epub', 'txt', 'both'):
            options['novel_format'] = raw_options['novelFormat']

        # 差分DL（Step 5）: 既にダウンロード済みの作品をスキップし、新規のみ取得する。
        # 判定はホスト側の正典DB（core.py の new_only ロジック）に委譲する。
        # work 種別のときは「その作品の作者の新規作品のみ」という意味になる（作者は要塞側で解決）。
        new_only = bool(data.get('newOnly', False))

        job_id = uuid.uuid4().hex
        logger.info(f"[アプリ連携] ダウンロード命令を受信しました: kind={kind} id={target_id} new_only={new_only} options={options}")
        try:
            from gui import gui_queue_log_callback
            if gui_queue_log_callback and gui_queue_log_callback[0]:
                label = "差分DL" if new_only else "DL"
                gui_queue_log_callback[0](f"[アプリ連携] {label}命令を受信: {kind} (id={target_id})", color="#00FF66")
        except Exception as e:
            logger.debug(f"GUI通知エラー: {e}")

        self.server.register_web_job(job_id, kind, target_id)
        self.server.web_download_queue.put({'jobId': job_id, 'kind': kind, 'id': target_id, 'options': options, 'new_only': new_only})
        self._send_response(200, {"jobId": job_id})

    def _handle_bookmark(self):
        """iOS版連携用: 実ブックマーク書き込みエンドポイント（Q5のB案、`/bookmark`）。
        iOSは自分のCookieで直接pixivへPOSTする経路(A)を主に使うため、こちらはその代替経路。
        `pixiv_client.py`の`follow_user()`と同じ`bookmark_add.php`土台を使う
        `add_illust_bookmark`/`add_novel_bookmark`へ委譲する。
        ⚠️ 実際にユーザーのpixivアカウントへ書き込む操作のため、まだ実機（実アカウント）検証は
        行っていない（`DirectPixivSource.addBookmark`と同様、本番投入前に必ず実機検証すること）。"""
        if not self._check_web_bridge_enabled():
            return
        if not self._check_web_token():
            return
        self.server.mark_app_contact()

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        try:
            data = json.loads(post_data.decode('utf-8'))
        except json.JSONDecodeError:
            self._send_response(400, {"status": "error", "message": "Invalid JSON"})
            return

        target = data.get('target') or {}
        kind = target.get('kind')
        target_id = target.get('id')
        is_private = bool(target.get('isPrivate', False))

        if kind not in ('illust', 'novel'):
            self._send_response(400, {"status": "error", "message": "Invalid target.kind"})
            return
        if not target_id:
            self._send_response(400, {"status": "error", "message": "Missing target.id"})
            return

        logger.info(f"[アプリ連携] ブックマーク命令を受信しました: kind={kind} id={target_id}")
        try:
            from gui import gui_queue_log_callback
            if gui_queue_log_callback and gui_queue_log_callback[0]:
                gui_queue_log_callback[0](f"[アプリ連携] ブックマーク命令を受信: {kind} (id={target_id})", color="#00FF66")
        except Exception as e:
            logger.debug(f"GUI通知エラー: {e}")

        client = PixivClient(db=self.server.db)
        restrict = 1 if is_private else 0
        try:
            if kind == 'illust':
                client.add_illust_bookmark(target_id, restrict=restrict)
            else:
                client.add_novel_bookmark(target_id, restrict=restrict)
            self._send_response(200, {"status": "ok"})
        except Exception as e:
            logger.exception(f"[アプリ連携] ブックマーク処理に失敗しました: {e}")
            self._send_response(500, {"status": "error", "message": str(e)})

    def _handle_web_progress(self):
        """iOS版 (HostSource.watchProgress) 用の SSE ストリーム。
        EventSource はカスタムヘッダを送れないため、トークンはクエリパラメータで受け取る。"""
        origin = self.headers.get('Origin', '')
        if not self._is_allowed_web_origin(origin):
            self._send_response(403, {"status": "error", "message": "Forbidden"})
            return
        if not self._check_web_bridge_enabled():
            return

        parsed = urllib.parse.urlsplit(self.path)
        job_id = parsed.path[len('/progress/'):]
        query = urllib.parse.parse_qs(parsed.query)
        token = (query.get('token') or [''])[0]
        if not token or token != self.server.get_web_token():
            self._send_response(401, {"status": "error", "message": "Invalid or missing token"})
            return

        if self.server.get_web_job(job_id) is None:
            self._send_response(404, {"status": "error", "message": "Unknown job"})
            return

        self.server.mark_app_contact()

        # 注意: ここで Connection: keep-alive ヘッダを送ると BaseHTTPRequestHandler.send_header() が
        # self.close_connection = False を内部設定してしまい、ストリーム終了後は handle() が
        # 同じソケットから次のリクエストを待ち続けて接続が塞がったままになる(クライアント側の
        # タイムアウトまでハングして実機検証で発覚)。SSE は単発の長時間レスポンスなので、
        # 明示的に close_connection = True にして終了後は必ずソケットを閉じさせる。
        self.close_connection = True
        self.send_response(200)
        self._send_cors_headers(origin)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()

        last_sent = None
        try:
            while True:
                job = self.server.get_web_job(job_id)
                if job is None:
                    break
                snapshot = json.dumps(job)
                if snapshot != last_sent:
                    self.wfile.write(f"data: {snapshot}\n\n".encode('utf-8'))
                    self.wfile.flush()
                    last_sent = snapshot
                if job['state'] in ('done', 'error'):
                    break
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_response(self, status_code, json_dict):
        self.send_response(status_code)
        self._send_cors_headers(self.headers.get('Origin', ''))
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(json_dict).encode('utf-8'))

    def log_message(self, format, *args):
        # http.server のデフォルトログ出力をロガーに流す
        logger.debug("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format % args))


_last_gui_sync_notify_time = [0]
_last_phpsessid = [None]

def save_cookies_from_extension(cookies_list):
    import os
    import time
    lines = [
        "# Netscape HTTP Cookie File",
        "# http://curl.haxx.se/docs/http-cookies.html",
        "# This is a generated file by PixivVault Extension! Do not edit.",
        ""
    ]

    old_cookies_map = {}
    if os.path.exists("cookies.txt"):
        try:
            with open("cookies.txt", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.strip().split('\t')
                    if len(parts) >= 7:
                        dom, sub, pth, sec, exp, nm, val = parts[:7]
                        old_cookies_map[(dom, pth, nm)] = (val, sec, exp)
        except Exception:
            old_cookies_map = {}

    has_pixiv = False
    new_cookies_map = {}
    new_phpsessid = None

    # (domain, path, name) で確実にソートして決定的な順序にする
    sorted_cookies = sorted(cookies_list, key=lambda c: (c.get('domain', ''), c.get('path', '/'), c.get('name', '')))

    for c in sorted_cookies:
        domain = c.get('domain', '')
        if 'pixiv.net' in domain:
            has_pixiv = True
        include_sub = "TRUE" if domain.startswith('.') else "FALSE"
        path = c.get('path', '/')
        secure = "TRUE" if c.get('secure', False) else "FALSE"
        name = c.get('name', '')
        value = c.get('value', '')

        if name == "PHPSESSID":
            new_phpsessid = value

        exp = c.get('expirationDate')
        if exp is not None and isinstance(exp, (int, float)) and exp > 0:
            expires_ts = int(exp)
        else:
            # セッションCookieの場合、実質的な値が変わっていないなら既存の有効期限タイムスタンプを維持する。
            # 変更がある場合のみ、12時間単位で丸めた有効期限にして毎秒の変動を防ぐ。
            if (domain, path, name) in old_cookies_map and old_cookies_map[(domain, path, name)][0] == value:
                expires_ts = int(old_cookies_map[(domain, path, name)][2])
            else:
                expires_ts = (int(time.time()) // 43200) * 43200 + 86400 * 30

        new_cookies_map[(domain, path, name)] = (value, secure, str(expires_ts))
        lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expires_ts}\t{name}\t{value}")

    if not has_pixiv:
        return False, "Pixiv cookies not found"

    # 全く同じ Cookie(ドメイン, パス, 名前, 値, セキュア, 有効期限) の内容なら変更なしとしてスキップ！
    if old_cookies_map == new_cookies_map:
        return True, "No changes"

    new_content = "\n".join(lines) + "\n"
    temp_file = "cookies.txt.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        f.write(new_content)
    os.replace(temp_file, "cookies.txt")

    # ログ出力＆GUIコールバック
    # 実質的なCookie内容が変わった場合であっても、フォーカスや画面遷移のたびに連続してGUIログや
    # ログイン状態確認が連発されないよう、PHPSESSIDが変わった時または前回通知から5分(300秒)以上経過した時のみ通知を行う。
    now = time.time()
    if new_phpsessid != _last_phpsessid[0] or (now - _last_gui_sync_notify_time[0]) >= 300:
        _last_phpsessid[0] = new_phpsessid
        _last_gui_sync_notify_time[0] = now
        try:
            from gui import gui_queue_log_callback, gui_trigger_cookie_check
            if gui_queue_log_callback and gui_queue_log_callback[0]:
                gui_queue_log_callback[0]("[Cookie Auto-Sync] 拡張機能から最新のCookieを自動同期しました", color="#00FF66")
            if gui_trigger_cookie_check and gui_trigger_cookie_check[0]:
                gui_trigger_cookie_check[0]()
        except Exception as e:
            logger.debug(f"GUI通知エラー: {e}")

    return True, "OK"



class PixivVaultServer(ThreadingHTTPServer):
    """/progress の SSE ストリームはジョブ完了までリクエストハンドラを長時間ブロックするため、
    素の HTTPServer (シングルスレッド・逐次処理) のままだと SSE 接続中は他の全リクエスト
    (拡張機能連携含む) が詰まってしまう。ThreadingHTTPServer で接続ごとにスレッドを分離する。"""
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, db, client):
        super().__init__(server_address, RequestHandlerClass)
        self.db = db
        self.client = client
        self.download_queue = queue.Queue()
        # キュー投入済み(未処理)または処理中のジョブキーを保持し、同一ジョブの多重投入を防ぐ。
        self._queued_keys = set()
        self._queued_keys_lock = threading.Lock()
        self.worker_thread = threading.Thread(target=self._queue_worker, daemon=True)
        self.worker_thread.start()

        # iOS版（要塞ホストモード） 用: 既存の enqueue()/_queued_keys 方式とは独立したキュー。
        # リクエストごとに一意な jobId を発行し、進捗を web_jobs で追跡する (SSE配信用)。
        self.web_download_queue = queue.Queue()
        self.web_jobs = {}
        self._web_jobs_lock = threading.Lock()
        self.web_worker_thread = threading.Thread(target=self._web_queue_worker, daemon=True)
        self.web_worker_thread.start()

        # iOS版が最後にこの要塞へ接続してきた時刻。GUI側のインジケータ表示・
        # 「アプリと接続しました」ログの重複抑制(直近60秒以内の再接続はログを出さない)に使う。
        self._last_app_contact_at = None
        self._app_contact_lock = threading.Lock()

    def mark_app_contact(self):
        """iOS版からの認証済みアクセスを検知するたびに呼ぶ。
        直近60秒以内に既に検知済みなら「新規接続」とはみなさずログは出さないが、
        GUIインジケータ用のタイムスタンプは毎回更新する。"""
        now = time.time()
        with self._app_contact_lock:
            previous = self._last_app_contact_at
            self._last_app_contact_at = now
        is_new_connection = previous is None or (now - previous) > 60
        if is_new_connection:
            logger.info("[アプリ連携] アプリと接続しました")
            try:
                from gui import gui_queue_log_callback, gui_log_callback
                if gui_queue_log_callback and gui_queue_log_callback[0]:
                    gui_queue_log_callback[0]("[アプリ連携] アプリと接続しました", color="#00FF66")
                if gui_log_callback and gui_log_callback[0]:
                    gui_log_callback[0]("--- [アプリ連携] アプリと接続しました ---", color="#00FF66")
            except Exception as e:
                logger.debug(f"GUI通知エラー: {e}")
        try:
            from gui import gui_set_app_connected
            if gui_set_app_connected and gui_set_app_connected[0]:
                gui_set_app_connected[0](now)
        except Exception as e:
            logger.debug(f"GUI通知エラー: {e}")

    def last_app_contact_at(self):
        with self._app_contact_lock:
            return self._last_app_contact_at

    def enqueue(self, key, task):
        """同一keyが投入済み/処理中でなければキューに追加する。追加した場合Trueを返す。"""
        with self._queued_keys_lock:
            if key in self._queued_keys:
                return False
            self._queued_keys.add(key)
        task['key'] = key
        self.download_queue.put(task)
        return True

    def _queue_worker(self):
        while True:
            task = self.download_queue.get()
            try:
                if task['type'] == 'user':
                    self.trigger_user_backup(task['user_id'], task['new_only'])
                elif task['type'] == 'work':
                    self.trigger_work_backup(task['work_id'], task['is_novel'])
            except Exception as e:
                logger.error(f"Worker error: {e}")
            finally:
                with self._queued_keys_lock:
                    self._queued_keys.discard(task.get('key'))
                self.download_queue.task_done()

    # ── iOS版（要塞ホストモード） ─────────────────────────────────

    def is_web_bridge_enabled(self):
        """GUIのトグルでiOS版連携の接続受け付けを一時停止できるようにするための
        フラグ。DB設定 'web_bridge_enabled'（既定 '1'=有効）。サーバー自体は落とさず、
        テスト時に意図的な「未接続」状態を作れるようにする（拡張機能連携には影響しない）。"""
        return self.db.get_setting('web_bridge_enabled', '1') == '1'

    def set_web_bridge_enabled(self, enabled: bool):
        self.db.set_setting('web_bridge_enabled', '1' if enabled else '0')

    def get_web_token(self):
        """iOS版の共有トークン。初回アクセス時に自動生成しDBへ永続化する。
        iOS側のHostRelay設定画面で、このトークンをユーザー自身が入力する想定。
        GUIへの表示は呼び出し元（start_server）が起動のたびに行う。ここで即座に
        通知しようとすると、まだ main_window() が完了していない起動直後のタイミングと
        競合して黙って失われることがあるため、ここでは通知を試みない。"""
        token = self.db.get_setting('web_api_token', '')
        if not token:
            token = generate_web_token()
            self.db.set_setting('web_api_token', token)
            logger.info(f"[アプリ連携] Web接続トークンを新規発行しました: {token}")
        return token

    def register_web_job(self, job_id, kind, target_id):
        with self._web_jobs_lock:
            self.web_jobs[job_id] = {
                'jobId': job_id,
                'done': 0,
                'total': 1,
                'state': 'queued',
                'message': f'{kind} のダウンロードを待機中です'
            }

    def get_web_job(self, job_id):
        with self._web_jobs_lock:
            job = self.web_jobs.get(job_id)
            return dict(job) if job is not None else None

    def update_web_job(self, job_id, **fields):
        with self._web_jobs_lock:
            job = self.web_jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _web_queue_worker(self):
        while True:
            task = self.web_download_queue.get()
            job_id = task['jobId']
            kind = task['kind']
            target_id = task['id']
            try:
                self.update_web_job(job_id, state='running', message='開始しました')
                self._run_web_download_job(job_id, kind, target_id, task.get('options'), task.get('new_only', False))
                self.update_web_job(job_id, state='done', message='完了しました')
                job = self.get_web_job(job_id)
                if job is not None:
                    self.update_web_job(job_id, done=job.get('total', 1))
                self._notify_web_job_result(kind, target_id, success=True)
            except Exception as e:
                logger.exception(f"[アプリ連携] ジョブ失敗 ({job_id}): {e}")
                self.update_web_job(job_id, state='error', message=str(e))
                self._notify_web_job_result(kind, target_id, success=False, error_message=str(e))
            finally:
                self.web_download_queue.task_done()

    def _notify_web_job_result(self, kind, target_id, success, error_message=None):
        """個別DL等と同じ「--- ...完了しました ---」形式のログを、キューログだけでなく
        メインログにも出す。従来はキューログにのみ受信ログが出て終わっていたため、
        実際にダウンロードが進行/完了しているかが分かりにくいという指摘を受けて追加した。"""
        if success:
            queue_message = f"[アプリ連携] ダウンロード完了: {kind} (id={target_id})"
            main_message = f"--- [アプリ連携] ダウンロードが完了しました: {kind} (id={target_id}) ---"
            color = "#00FF66"
            logger.info(main_message)
        else:
            queue_message = f"[アプリ連携] ダウンロード失敗: {kind} (id={target_id}): {error_message}"
            main_message = f"--- [アプリ連携] ダウンロードに失敗しました: {kind} (id={target_id}): {error_message} ---"
            color = "#FF5555"
            logger.error(main_message)
        try:
            from gui import gui_queue_log_callback, gui_log_callback
            if gui_queue_log_callback and gui_queue_log_callback[0]:
                gui_queue_log_callback[0](queue_message, color=color)
            if gui_log_callback and gui_log_callback[0]:
                gui_log_callback[0](main_message, color=color)
        except Exception as e:
            logger.debug(f"GUI通知エラー: {e}")

    def _run_web_download_job(self, job_id, kind, target_id, options=None, new_only=False):
        # Cookie更新をアプリ再起動なしに反映させるため、都度クライアントを生成する
        client = PixivClient(db=self.db)
        options = options or {}

        def log_cb(msg, color=None):
            logger.debug(f"[アプリ連携] {msg}")

        def progress_cb(idx, total, elapsed):
            self.update_web_job(job_id, done=idx, total=total, message=f'{idx}/{total} 件処理中')

        if kind == 'work':
            if new_only:
                # 差分DL: この作品の「作者」を要塞側で解決し、その作者の新規作品のみ取得する
                # （拡張は作品IDしか持たないため作者解決はここで行う。PC版拡張の「差分DL」＝
                # 作者単位new_onlyと同じ意味論）。
                work = client.get_work_info(target_id) or client.get_novel_info(target_id)
                if not work:
                    raise Exception(f'作品ID {target_id} が見つかりませんでした')
                author_id = work.get('user_id')
                if not author_id:
                    raise Exception(f'作品ID {target_id} の作者を特定できませんでした')
                self.update_web_job(job_id, message=f"作者 {work.get('user_name', author_id)} の新規作品を確認中")
                run_backup(
                    user_id=str(author_id), client=client, db=self.db, target_type='both',
                    log_callback=log_cb, progress_callback=progress_cb, new_only=True, options=options
                )
            else:
                work = client.get_work_info(target_id)
                is_novel = False
                if not work:
                    work = client.get_novel_info(target_id)
                    is_novel = True
                if not work:
                    raise Exception(f'作品ID {target_id} が見つかりませんでした')
                self.update_web_job(job_id, total=1, message=f"{work.get('title', '無題')} を保存中")
                run_single_work_backup(work_id=target_id, is_novel=is_novel, client=client, db=self.db, log_callback=log_cb, options=options)

        elif kind == 'author':
            run_backup(
                user_id=target_id, client=client, db=self.db, target_type='both',
                log_callback=log_cb, progress_callback=progress_cb, new_only=new_only, options=options
            )

        elif kind == 'bookmarks':
            my_user_id = client.get_my_user_id()
            if not my_user_id:
                raise Exception('ログイン状態を確認できませんでした（cookies.txtをご確認ください）')
            download_bookmarks(
                db=self.db, client=client, my_user_id=my_user_id, target_type='both', rest_type='show',
                log_callback=log_cb, progress_callback=progress_cb, options=options
            )

        elif kind == 'following':
            my_user_id = client.get_my_user_id()
            if not my_user_id:
                raise Exception('ログイン状態を確認できませんでした（cookies.txtをご確認ください）')
            following = client.get_following_users(my_user_id)
            user_ids = [u['user_id'] for u in following if u.get('user_id')]
            if not user_ids:
                raise Exception('フォロー中のユーザーが見つかりませんでした')

            def batch_progress_cb(idx, total, user_id, elapsed):
                self.update_web_job(job_id, done=idx, total=total, message=f'{idx}/{total} 人目: {user_id}')

            run_batch_backup(
                user_ids=user_ids, client=client, db=self.db, target_type='both',
                log_callback=log_cb, progress_callback=progress_cb,
                batch_progress_callback=batch_progress_cb, new_only=new_only, options=options
            )
        else:
            raise Exception(f'不明な target.kind: {kind}')

    def notify(self, title, message):
        try:
            if self.db.get_setting("enable_notifications", "1") == "1":
                threading.Thread(target=lambda: send_notification(title, message), daemon=True).start()
        except Exception as e:
            logger.error(f"通知の送信に失敗しました: {e}")

    def trigger_user_backup(self, user_id, new_only=False):
        flow_name = f"ext_user_{user_id}"
        try:
            from gui import gui_set_flow_active
            if gui_set_flow_active[0]:
                gui_set_flow_active[0](flow_name, True)
        except Exception:
            pass
        try:
            self.notify("PixivVault ダウンロード開始", f"ユーザーID: {user_id} のバックアップを開始します。")
            def log_cb(msg, color=None):
                logger.debug(f"[拡張機能連携] {msg}")
                try:
                    from gui import gui_queue_log_callback
                    if gui_queue_log_callback and gui_queue_log_callback[0]:
                        if color:
                            gui_queue_log_callback[0](msg, color)
                        else:
                            gui_queue_log_callback[0](msg)
                except Exception as e:
                    logger.debug(f"GUIキューログ転送エラー: {e}")

            # Cookie更新をアプリ再起動なしに反映させるため、起動時に使い回すのではなく都度生成する
            client = PixivClient(db=self.db)
            log_cb(f"=== ユーザーID: {user_id} のバックアップ開始 ===")

            run_backup(user_id=user_id, client=client, db=self.db, target_type="both", log_callback=log_cb, new_only=new_only)

            self.notify("PixivVault ダウンロード完了", f"ユーザーID: {user_id} のバックアップが完了しました！")
            log_cb(f"=== ユーザーID: {user_id} 完了 ===")
        except Exception as e:
            logger.exception(f"拡張機能からのユーザーダウンロードに失敗 (ID: {user_id}): {e}")
            self.notify("PixivVault エラー", f"ダウンロードに失敗しました: {e}")
            try:
                from gui import gui_queue_log_callback
                if gui_queue_log_callback and gui_queue_log_callback[0]:
                    gui_queue_log_callback[0](f"[エラー] ユーザー {user_id}: {e}", color="red")
            except Exception:
                pass
        finally:
            try:
                from gui import gui_set_flow_active
                if gui_set_flow_active[0]:
                    gui_set_flow_active[0](flow_name, False)
            except Exception:
                pass

    def trigger_work_backup(self, work_id, is_novel):
        flow_name = f"ext_work_{work_id}"
        try:
            from gui import gui_set_flow_active
            if gui_set_flow_active[0]:
                gui_set_flow_active[0](flow_name, True)
        except Exception:
            pass
        try:
            type_str = "小説" if is_novel else "イラスト/マンガ"
            self.notify("PixivVault ダウンロード開始", f"{type_str} ID: {work_id} の保存を開始します。")

            def log_cb(msg, color=None):
                logger.debug(f"[拡張機能連携] {msg}")
                try:
                    from gui import gui_queue_log_callback
                    if gui_queue_log_callback and gui_queue_log_callback[0]:
                        if color:
                            gui_queue_log_callback[0](msg, color)
                        else:
                            gui_queue_log_callback[0](msg)
                except Exception as e:
                    logger.debug(f"GUIキューログ転送エラー: {e}")

            # Cookie更新をアプリ再起動なしに反映させるため、起動時に使い回すのではなく都度生成する
            client = PixivClient(db=self.db)
            log_cb(f"=== {type_str} ID: {work_id} の保存開始 ===")

            run_single_work_backup(work_id=work_id, is_novel=is_novel, client=client, db=self.db, log_callback=log_cb)

            self.notify("PixivVault ダウンロード完了", f"作品の保存が完了しました！")
            log_cb(f"=== {type_str} ID: {work_id} 完了 ===")
        except Exception as e:
            logger.exception(f"拡張機能からの作品ダウンロードに失敗 (ID: {work_id}): {e}")
            self.notify("PixivVault エラー", f"ダウンロードに失敗しました: {e}")
            try:
                from gui import gui_queue_log_callback
                if gui_queue_log_callback and gui_queue_log_callback[0]:
                    gui_queue_log_callback[0](f"[エラー] 作品 {work_id}: {e}", color="red")
            except Exception:
                pass
        finally:
            try:
                from gui import gui_set_flow_active
                if gui_set_flow_active[0]:
                    gui_set_flow_active[0](flow_name, False)
            except Exception:
                pass

def start_server(port, db, client):
    # 0.0.0.0: iOS版(自宅LAN直結モード)がLAN内の他端末からこのサーバーへ到達できるようにするため、
    # 2026-07-22にループバック限定から変更。拡張機能専用エンドポイントは_is_allowed_extension_origin
    # 側でループバック接続元のみへ引き続き限定しているため、LAN上の他端末からは到達不可のまま。
    server_address = ('0.0.0.0', port)
    httpd = PixivVaultServer(server_address, PixivVaultRequestHandler, db, client)
    logger.info(f"拡張機能連携サーバーを 0.0.0.0:{port} (LAN含む全インターフェース) で起動しました。")
    token = httpd.get_web_token()
    logger.info(f"[アプリ連携] 接続トークン: {token}")
    # 新規発行時は get_web_token() 内の _announce_web_token() で既にGUIへ表示されているが、
    # 既存トークンを使い回す場合は何も表示されず「トークンがどこにも見当たらない」状態に
    # なってしまうため、既存/新規を問わず起動のたびに必ずキューログへ表示する。
    #
    # 注意: この関数は main.py が起動直後にバックグラウンドスレッドとして呼び出すため、
    # Flet の main_window() がまだ完了しておらず gui_queue_log_callback[0] が None のままの
    # 可能性が高い（Fletデスクトップクライアントの起動には数秒かかることがある）。
    # 即座に1回だけ試すと通知が黙って失われるため、コールバックが登録されるまで
    # 短い間隔でリトライする（このスレッド自体は serve_forever をブロックしないよう分離する）。
    def _announce_startup_token():
        for _ in range(100):  # 0.2秒 x 100 = 最大約20秒待つ
            try:
                from gui import gui_queue_log_callback
                if gui_queue_log_callback and gui_queue_log_callback[0]:
                    gui_queue_log_callback[0](f"[アプリ連携] 接続トークン: {token}", color="#00FF66")
                    return
            except Exception as e:
                logger.debug(f"GUI通知エラー: {e}")
                return
            time.sleep(0.2)
    threading.Thread(target=_announce_startup_token, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
