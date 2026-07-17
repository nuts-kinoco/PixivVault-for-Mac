import time
import random
import logging
import re
import threading
import hashlib
from typing import List, Dict, Any, Callable

import requests

logger = logging.getLogger(__name__)

class PixivClient:
    def __init__(self, db=None):
        """ログイン状態の確認とCookieの読み込みを行います。"""
        if db is None:
            try:
                from database import Database
                self.db = Database()
            except Exception:
                self.db = None
        else:
            self.db = db
        self.session = requests.Session()
        # PixivのAPIを叩くための基本的なヘッダーを設定します。
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
            'Referer': 'https://www.pixiv.net/',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
        })
        self._memory_cache = {}
        import os
        self._cache_dir = os.path.join(os.getcwd(), "cache", "thumbnails")
        os.makedirs(self._cache_dir, exist_ok=True)
        self._load_cookies()

    def get_rate_settings(self):
        """レート制限・速度制御設定（待機秒数、リトライ上限、429待機秒数）を取得します"""
        interval, max_retries, retry_wait = 1.5, 3, 5.0
        if self.db:
            try:
                interval = float(self.db.get_setting("download_interval", "1.5"))
            except Exception:
                interval = 1.5
            try:
                max_retries = int(self.db.get_setting("api_retry_count", "3"))
            except Exception:
                max_retries = 3
            try:
                retry_wait = float(self.db.get_setting("api_retry_wait", "5.0"))
            except Exception:
                retry_wait = 5.0
        return interval, max_retries, retry_wait

    def _load_cookies(self):
        import os
        import http.cookiejar
        
        cookie_file = 'cookies.txt'
        
        if not os.path.exists(cookie_file):
            logger.error(f"「{cookie_file}」が見つかりません。")
            logger.warning(
                "自動取得がWindowsのセキュリティにブロックされてしまうため、手動エクスポート方式に変更しました。\n"
                "1. Chrome等のブラウザでPixivにログインします。\n"
                "2. 拡張機能「Get cookies.txt LOCALLY」等を使ってCookieをエクスポートします。\n"
                f"3. ダウンロードしたファイルを '{cookie_file}' という名前で、main.py と同じフォルダに配置してください。"
            )
            return
            
        try:
            cj = http.cookiejar.MozillaCookieJar(cookie_file)
            cj.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies.update(cj)
            
            # PixivのCookieが入っているか軽く確認します
            has_pixiv = any('pixiv.net' in cookie.domain for cookie in cj)
            if not has_pixiv:
                logger.warning(f"「{cookie_file}」にPixivのCookieが含まれていない可能性があります。")
                
            logger.info(f"{cookie_file} からPixivのCookieを読み込みました。")
        except Exception as e:
            logger.error(f"Cookieファイルの読み込みに失敗しました: {e}")
            raise

    @classmethod
    def check_cookie_status(cls, cookie_file: str = "cookies.txt") -> dict:
        """Cookieファイルの状態と有効期限を検証します。

        戻り値:
            status: "valid" | "warning_yellow" | "warning_red" | "expired" | "missing"
            message: 表示用メッセージ
            expires_at: セッションCookieの有効期限日時 (ISO string または None)
            user_id: ログイン中ユーザーID (取得できた場合)
            days_left: 有効期限までの残り日数
        """
        import os
        import http.cookiejar
        from datetime import datetime

        if not os.path.exists(cookie_file):
            return {
                "status": "missing",
                "message": "cookies.txt が見つかりません。設定からインポートしてください。",
                "expires_at": None,
                "user_id": None,
                "days_left": 0
            }

        try:
            cj = http.cookiejar.MozillaCookieJar(cookie_file)
            cj.load(ignore_discard=True, ignore_expires=True)

            pixiv_cookies = [c for c in cj if 'pixiv.net' in c.domain]
            if not pixiv_cookies:
                return {
                    "status": "expired",
                    "message": "cookies.txt にPixivのCookieが含まれていません。",
                    "expires_at": None,
                    "user_id": None,
                    "days_left": 0
                }

            # ログインセッションの核である PHPSESSID を優先取得（関係ないトラッキングCookieの期限切れで判定しない）
            phpsessid = next((c for c in pixiv_cookies if c.name == 'PHPSESSID'), None)
            target_expires = None
            if phpsessid and phpsessid.expires and phpsessid.expires > 0:
                target_expires = phpsessid.expires
            else:
                # PHPSESSID に有効期限がない場合は他の主要セッションCookieの中で最大の有効期限を使用
                valid_expires = [c.expires for c in pixiv_cookies if c.expires and c.expires > 0 and c.name not in ['_cfuvid', '_ga', 'g_state']]
                if valid_expires:
                    target_expires = max(valid_expires)

            now_ts = time.time()
            expires_iso = datetime.fromtimestamp(target_expires).isoformat() if target_expires else None

            # 実際のAPIでログインが有効かテスト（確実な検証）
            client = cls()
            user_id = client.get_my_user_id()
            if not user_id:
                return {
                    "status": "expired",
                    "message": "ログインセッションが無効化されています。またはCookieの有効期限が切れています。",
                    "expires_at": expires_iso,
                    "user_id": None,
                    "days_left": 0
                }

            # API認証成功時の残り日数計算
            import math
            if target_expires and target_expires > now_ts:
                days_left = max(1, int(math.ceil((target_expires - now_ts) / 86400)))
            else:
                # APIは通るが有効期限タイムスタンプが過去/未設定のセッションCookieの場合、デフォルト有効扱い
                days_left = 30

            # 残り8日以上なら緑(valid)、残り4日～7日(1週間)なら黄色(warning_yellow)、残り3日以下で赤(warning_red)
            if days_left > 7:
                return {
                    "status": "valid",
                    "message": f"ログイン中 (有効期限 残り約{days_left}日) - ユーザーID: {user_id}",
                    "expires_at": expires_iso,
                    "user_id": str(user_id),
                    "days_left": days_left
                }
            elif days_left > 3:
                return {
                    "status": "warning_yellow",
                    "message": f"Cookie有効期限 残り約{days_left}日 (1週間以内/黄色アラート) - ID: {user_id}",
                    "expires_at": expires_iso,
                    "user_id": str(user_id),
                    "days_left": days_left
                }
            else:
                return {
                    "status": "warning_red",
                    "message": f"Cookie有効期限 残り約{days_left}日 (3日以内/赤色アラート) - ID: {user_id}",
                    "expires_at": expires_iso,
                    "user_id": str(user_id),
                    "days_left": days_left
                }
        except Exception as e:
            return {
                "status": "expired",
                "message": f"Cookie検証失敗または期限切れ ({str(e)})",
                "expires_at": None,
                "user_id": None,
                "days_left": 0
            }

    @classmethod
    def _extract_cookies_native(cls, browser_name: str) -> List[dict]:
        """OS標準の資格情報ストアを使った Chrome/Edge/Brave の暗号化 Cookie 抽出エンジン
        (Windows: pycryptodome + win32crypt / DPAPI、macOS: pycryptodome + Keychain)"""
        import sys
        import os
        import shutil
        import sqlite3
        import tempfile

        if sys.platform == "win32":
            import json
            import base64
            try:
                import win32crypt
                from Crypto.Cipher import AES
            except ImportError:
                return []

            local_app_data = os.environ.get('LOCALAPPDATA', '')
            profiles = {
                "Chrome": os.path.join(local_app_data, r"Google\Chrome\User Data"),
                "Edge": os.path.join(local_app_data, r"Microsoft\Edge\User Data"),
                "Brave": os.path.join(local_app_data, r"BraveSoftware\Brave-Browser\User Data")
            }

            user_data_dir = profiles.get(browser_name)
            if not user_data_dir or not os.path.exists(user_data_dir):
                return []

            # 1. マスターキーの復元
            local_state_path = os.path.join(user_data_dir, "Local State")
            if not os.path.exists(local_state_path):
                return []

            try:
                with open(local_state_path, "r", encoding="utf-8") as f:
                    local_state = json.load(f)
                encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key")
                if not encrypted_key_b64:
                    return []
                encrypted_key = base64.b64decode(encrypted_key_b64)
                if encrypted_key.startswith(b"DPAPI"):
                    encrypted_key = encrypted_key[5:]
                master_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
            except Exception as ex:
                logger.debug(f"{browser_name} キー復元エラー: {ex}")
                return []

            def _decrypt(enc_val: bytes) -> str:
                if enc_val.startswith(b"v10") or enc_val.startswith(b"v20"):
                    nonce = enc_val[3:15]
                    ciphertext = enc_val[15:-16]
                    tag = enc_val[-16:]
                    cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
                    return cipher.decrypt_and_verify(ciphertext, tag).decode('utf-8')
                return win32crypt.CryptUnprotectData(enc_val, None, None, None, 0)[1].decode('utf-8')

            # 2. 各プロファイルの Cookies データベースから pixiv.net 関連のレコードを解読
            search_profiles = ["Default"] + [f"Profile {i}" for i in range(1, 10)]
            profile_dirs = [(prof, os.path.join(user_data_dir, prof)) for prof in search_profiles]

        elif sys.platform == "darwin":
            try:
                from Crypto.Cipher import AES
                from Crypto.Protocol.KDF import PBKDF2
            except ImportError:
                return []

            keychain_info = {
                "Chrome": ("Chrome Safe Storage", "Chrome"),
                "Edge": ("Microsoft Edge Safe Storage", "Microsoft Edge"),
                "Brave": ("Brave Safe Storage", "Brave"),
            }.get(browser_name)
            if not keychain_info:
                return []
            service, account = keychain_info

            try:
                import subprocess
                result = subprocess.run(
                    ["security", "find-generic-password", "-w", "-a", account, "-s", service],
                    capture_output=True, text=True, timeout=15
                )
                if result.returncode != 0 or not result.stdout.strip():
                    return []
                safe_storage_password = result.stdout.strip()
            except Exception as ex:
                logger.debug(f"{browser_name} Keychainアクセスエラー: {ex}")
                return []

            master_key = PBKDF2(safe_storage_password, b'saltysalt', dkLen=16, count=1003)
            iv = b' ' * 16

            def _decrypt(enc_val: bytes) -> str:
                if enc_val[:3] not in (b"v10", b"v11"):
                    raise ValueError("unsupported cookie encryption prefix")
                cipher = AES.new(master_key, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(enc_val[3:])
                pad_len = decrypted[-1]
                return decrypted[:-pad_len].decode('utf-8')

            home = os.path.expanduser("~")
            profiles_root = {
                "Chrome": os.path.join(home, "Library/Application Support/Google/Chrome"),
                "Edge": os.path.join(home, "Library/Application Support/Microsoft Edge"),
                "Brave": os.path.join(home, "Library/Application Support/BraveSoftware/Brave-Browser"),
            }.get(browser_name)
            if not profiles_root or not os.path.exists(profiles_root):
                return []

            search_profiles = ["Default"] + [f"Profile {i}" for i in range(1, 10)]
            profile_dirs = [(prof, os.path.join(profiles_root, prof)) for prof in search_profiles]

        else:
            return []

        found_cookies = []

        for prof, prof_dir in profile_dirs:
            db_path = os.path.join(prof_dir, "Network", "Cookies")
            if not os.path.exists(db_path):
                db_path = os.path.join(prof_dir, "Cookies")
            if not os.path.exists(db_path):
                continue

            temp_db = os.path.join(tempfile.gettempdir(), f"pixiv_cookies_{browser_name}_{prof}.db")
            try:
                shutil.copy2(db_path, temp_db)
                conn = sqlite3.connect(temp_db)
                cursor = conn.cursor()
                cursor.execute("SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure FROM cookies WHERE host_key LIKE '%pixiv%'")
                rows = cursor.fetchall()
                conn.close()
                os.remove(temp_db)

                for host, name, val, enc_val, path, expires_utc, is_secure in rows:
                    value = val
                    if not value and enc_val:
                        try:
                            value = _decrypt(enc_val)
                        except Exception:
                            continue

                    if value:
                        if expires_utc and expires_utc > 0:
                            expires_unix = int((expires_utc / 1000000) - 11644473600)
                        else:
                            expires_unix = int(time.time() + 86400 * 30)

                        found_cookies.append({
                            "domain": host,
                            "name": name,
                            "value": value,
                            "path": path,
                            "expires": expires_unix,
                            "secure": bool(is_secure)
                        })
            except Exception as ex:
                logger.debug(f"{browser_name} ({prof}) 解読エラー: {ex}")
                continue

        return found_cookies

    @classmethod
    def auto_extract_browser_cookies(cls, target_file: str = "cookies.txt") -> dict:
        """主要ブラウザ (Chrome, Edge, Firefox, Brave) から Pixiv の Cookie を全自動抽出して Netscape 形式で保存します"""
        import sys, os, glob, sqlite3, tempfile, shutil
        browsers = ["Chrome", "Edge", "Brave"]
        extracted_cookies = None
        used_browser = ""

        # 1. Chrome, Edge, Brave のネイティブ解読
        for b_name in browsers:
            c_list = cls._extract_cookies_native(b_name)
            if c_list and any(c.get('name') == 'PHPSESSID' for c in c_list):
                extracted_cookies = c_list
                used_browser = b_name
                break

        # 2. Firefox の探索（暗号化無しで格納されている cookies.sqlite から取得）
        if not extracted_cookies:
            if sys.platform == "win32":
                app_data = os.environ.get('APPDATA', '')
                ff_glob_pattern = os.path.join(app_data, r"Mozilla\Firefox\Profiles\*\cookies.sqlite")
            elif sys.platform == "darwin":
                ff_glob_pattern = os.path.expanduser("~/Library/Application Support/Firefox/Profiles/*/cookies.sqlite")
            else:
                ff_glob_pattern = os.path.expanduser("~/.mozilla/firefox/*/cookies.sqlite")

            ff_profiles = glob.glob(ff_glob_pattern)
            for ff_db in ff_profiles:
                try:
                    temp_ff = os.path.join(tempfile.gettempdir(), "ff_cookies_temp.sqlite")
                    shutil.copy2(ff_db, temp_ff)
                    conn = sqlite3.connect(temp_ff)
                    cursor = conn.cursor()
                    cursor.execute("SELECT host, name, value, path, expiry, isSecure FROM moz_cookies WHERE host LIKE '%pixiv%'")
                    ff_rows = cursor.fetchall()
                    conn.close()
                    os.remove(temp_ff)

                    ff_list = []
                    for host, name, val, path, expiry, isSecure in ff_rows:
                        if val:
                            ff_list.append({
                                "domain": host,
                                "name": name,
                                "value": val,
                                "path": path,
                                "expires": int(expiry) if expiry else int(time.time() + 86400 * 30),
                                "secure": bool(isSecure)
                            })
                    if ff_list and any(c.get('name') == 'PHPSESSID' for c in ff_list):
                        extracted_cookies = ff_list
                        used_browser = "Firefox"
                        break
                except Exception as ex:
                    logger.debug(f"Firefox Cookie 抽出エラー: {ex}")

        if not extracted_cookies:
            return {
                "status": "expired",
                "message": "Chrome/Edge/Firefox 等のブラウザから Pixiv ログイン Cookie (PHPSESSID) の自動解読ができませんでした。普段お使いのブラウザで Pixiv にログイン後 [Cookie更新] を押すか、右上の [拡張機能] より cookies.txt を保存してください。",
                "expires_at": None,
                "user_id": None,
                "days_left": 0
            }

        # Netscape 形式へ保存
        try:
            with open(target_file, "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write(f"# Generated automatically by PixivVault (from {used_browser})\n\n")
                for c in extracted_cookies:
                    if isinstance(c, dict):
                        domain = c.get('domain', '.pixiv.net')
                        name = c.get('name', '')
                        value = c.get('value', '')
                        path = c.get('path', '/')
                        expires = c.get('expires', 0)
                        secure = c.get('secure', True)
                    else:
                        domain = getattr(c, 'domain', '.pixiv.net')
                        name = getattr(c, 'name', '')
                        value = getattr(c, 'value', '')
                        path = getattr(c, 'path', '/')
                        expires = getattr(c, 'expires', 0)
                        secure = getattr(c, 'secure', True)

                    if not domain.startswith('.'):
                        domain = '.' + domain

                    if not expires or expires <= 0:
                        expires = int(time.time() + 86400 * 30)
                    else:
                        expires = int(expires)

                    secure_str = "TRUE" if secure else "FALSE"
                    f.write(f"{domain}\tTRUE\t{path}\t{secure_str}\t{expires}\t{name}\t{value}\n")
        except Exception as e:
            return {
                "status": "expired",
                "message": f"Cookie ファイル ({target_file}) への自動書き込みに失敗しました: {e}",
                "expires_at": None,
                "user_id": None,
                "days_left": 0
            }

        res = cls.check_cookie_status(target_file)
        if res.get("status") in ["valid", "warning_yellow", "warning_red"]:
            res["message"] = f"[{used_browser}より自動取得] " + res["message"]
            res["browser"] = used_browser
        return res

    def _request_with_retry(self, url: str, params: dict = None, max_retries: int = None, method: str = 'GET', data: dict = None) -> dict:
        """APIリクエストを送信し、エラー時・429時は設定に基づき再試行します。"""
        interval, cfg_max_retries, retry_wait = self.get_rate_settings()
        if max_retries is None:
            max_retries = cfg_max_retries

        for attempt in range(max_retries):
            try:
                sleep_time = random.uniform(interval * 0.8, interval * 1.2) if interval > 0 else 0
                if sleep_time > 0:
                    msg = f"[API Wait] リクエスト前に {sleep_time:.2f} 秒待機します。"
                    if getattr(self, 'show_wait_log', False):
                        if getattr(self, 'log_callback', None):
                            self.log_callback(msg)
                        logger.info(msg)
                    else:
                        logger.debug(msg)
                    time.sleep(sleep_time)

                if method.upper() == 'POST':
                    response = self.session.post(url, params=params, data=data, timeout=10)
                else:
                    response = self.session.get(url, params=params, timeout=10)
                response.raise_for_status()

                response_data = response.json()
                if response_data.get('error'):
                    error_msg = response_data.get('message', '不明なエラー')
                    logger.error(f"Pixiv APIからエラーが返ってきました: {error_msg}")
                    raise Exception(f"Pixiv APIエラー: {error_msg}")

                # 通信完了時、セッションのCookieを cookies.txt へ自動同期・延長保存
                try:
                    if hasattr(self, 'cj') and self.cj:
                        for c in self.session.cookies:
                            self.cj.set_cookie(c)
                        self.cj.save(ignore_discard=True, ignore_expires=True)
                except Exception:
                    pass

                return response_data
            
            except (requests.RequestException, ValueError, Exception) as e:
                logger.warning(f"通信に失敗しました（{attempt + 1}/{max_retries}回目）: {e}")
                if attempt == max_retries - 1:
                    logger.error("最大リトライ回数に達しました。")
                    raise
                
                # 429 Too Many Requests 検知時は設定待機時間（retry_wait）を適用
                is_429 = (isinstance(e, requests.HTTPError) and getattr(e, 'response', None) is not None and e.response.status_code == 429)
                if is_429:
                    logger.warning(f"429 Too Many Requests 検知: {retry_wait} 秒待機してから再試行します。")
                    time.sleep(retry_wait)
                else:
                    backoff_time = (2 ** attempt) + random.uniform(0, 1)
                    logger.info(f"{backoff_time:.2f}秒後に再挑戦します。")
                    time.sleep(backoff_time)

    def get_user_work_count(self, user_id: str) -> int:
        """指定したユーザーIDの総作品数（イラスト＋マンガ＋小説）を高速に取得します。"""
        profile_url = f"https://www.pixiv.net/ajax/user/{user_id}/profile/all"
        try:
            profile_data = self._request_with_retry(profile_url)
            body = profile_data.get('body', {})
            if not isinstance(body, dict):
                return 0
            illusts = body.get('illusts', {}) or {}
            manga = body.get('manga', {}) or {}
            novels = body.get('novels', {}) or {}
            return len(illusts) + len(manga) + len(novels)
        except Exception as e:
            logger.warning(f"ユーザーID {user_id} の作品数取得に失敗しました: {e}")
            return 0

    def get_user_works(self, user_id: str, early_exit_checker: Callable[[Dict[str, Any]], bool] = None, known_ids_out: set = None) -> List[Dict[str, Any]]:
        """指定したユーザーIDの作品一覧を取得します。
        known_ids_out が指定された場合、Pixivの作品一覧(profile/all)に存在が確認できた
        全IDを追加します（詳細情報の取得に個別に失敗したIDも含む）。
        これは「削除されたかどうか」の判定を、詳細取得の一時的な失敗と混同しないようにするためです。
        """
        logger.info(f"ユーザーID「{user_id}」の作品一覧を取得します。")

        # 1. まずは全作品IDを一括で取得
        profile_url = f"https://www.pixiv.net/ajax/user/{user_id}/profile/all"
        profile_data = self._request_with_retry(profile_url)

        body = profile_data.get('body', {})
        if not isinstance(body, dict):
            logger.info("このユーザーは作品を公開していないか、非公開アカウントのようです。")
            return []

        illusts = body.get('illusts', {}) or {}
        manga = body.get('manga', {}) or {}

        # イラストとマンガのIDを統合して降順（新しい順）にソートします
        work_ids = list(illusts.keys()) + list(manga.keys())
        work_ids.sort(key=int, reverse=True)

        if known_ids_out is not None:
            known_ids_out.update(str(wid) for wid in work_ids)

        if not work_ids:
            logger.info("取得できる作品が見当たりませんでした。")
            return []

        logger.info(f"合計 {len(work_ids)} 件の作品IDが見つかりました。詳細情報を取得します。")

        # 2. ページネーションで詳細情報を取得 (Pixivの仕様上、一度に最大48件ずつ)
        works_list = []
        chunk_size = 48

        for i in range(0, len(work_ids), chunk_size):
            chunk_ids = work_ids[i:i + chunk_size]
            logger.info(f"進捗: {i + 1} 〜 {min(i + chunk_size, len(work_ids))} 件目を確認中...")

            illusts_url = f"https://www.pixiv.net/ajax/user/{user_id}/profile/illusts"
            params = {
                'work_category': 'illustManga',
                'is_first_page': 1 if i == 0 else 0,
            }
            # IDのリストを "ids[]" パラメータとして複数渡すための処理
            params['ids[]'] = chunk_ids

            details_data = self._request_with_retry(illusts_url, params=params)
            works = details_data.get('body', {}).get('works', {})

            for work_id in chunk_ids:
                work_info = works.get(str(work_id))
                if work_info:
                    parsed_work = {
                        'id': work_info.get('id'),
                        'title': work_info.get('title'),
                        'type': work_info.get('illustType', 0),
                        'user_name': work_info.get('userName', 'Unknown'),
                        'page_count': work_info.get('pageCount', 1),
                        'create_date': work_info.get('createDate', ''),
                        'update_date': work_info.get('updateDate', '')
                    }
                    if early_exit_checker and early_exit_checker(parsed_work):
                        logger.info("既に取得済みの作品に到達したため、以降の取得をスキップします。")
                        return works_list
                    works_list.append(parsed_work)
                else:
                    logger.warning(f"作品ID {work_id} は一覧に存在しますが詳細情報を取得できませんでした（一時的な取得失敗の可能性）。")

        logger.info(f"全 {len(works_list)} 件の作品データを取得しました。")
        return works_list

    def get_user_novels(self, user_id: str, early_exit_checker: Callable[[Dict[str, Any]], bool] = None, known_ids_out: set = None) -> List[Dict[str, Any]]:
        """指定したユーザーIDの小説一覧を取得します。
        known_ids_out が指定された場合、Pixivの作品一覧(profile/all)に存在が確認できた
        全IDを追加します（詳細情報の取得に個別に失敗したIDも含む）。
        """
        logger.info(f"ユーザーID「{user_id}」の小説一覧を取得します。")

        profile_url = f"https://www.pixiv.net/ajax/user/{user_id}/profile/all"
        profile_data = self._request_with_retry(profile_url)

        body = profile_data.get('body', {})
        if not isinstance(body, dict):
            return []

        novels = body.get('novels', {}) or {}

        work_ids = list(novels.keys())
        work_ids.sort(key=int, reverse=True)

        if known_ids_out is not None:
            known_ids_out.update(str(wid) for wid in work_ids)

        if not work_ids:
            return []

        logger.info(f"合計 {len(work_ids)} 件の小説IDが見つかりました。詳細情報を取得します。")

        works_list = []
        chunk_size = 48

        for i in range(0, len(work_ids), chunk_size):
            chunk_ids = work_ids[i:i + chunk_size]

            novels_url = f"https://www.pixiv.net/ajax/user/{user_id}/profile/novels"
            params = {
                'is_first_page': 1 if i == 0 else 0,
            }
            params['ids[]'] = chunk_ids

            details_data = self._request_with_retry(novels_url, params=params)
            works = details_data.get('body', {}).get('works', {})

            for work_id in chunk_ids:
                work_info = works.get(str(work_id))
                if work_info:
                    parsed_work = {
                        'id': work_info.get('id'),
                        'title': work_info.get('title'),
                        'type': 'novel',
                        'user_name': work_info.get('userName', 'Unknown'),
                        'text_count': work_info.get('textCount', 0),
                        'create_date': work_info.get('createDate', ''),
                        'update_date': work_info.get('updateDate', '')
                    }
                    if early_exit_checker and early_exit_checker(parsed_work):
                        logger.info("既に取得済みの小説に到達したため、以降の取得をスキップします。")
                        return works_list
                    works_list.append(parsed_work)
                else:
                    logger.warning(f"小説ID {work_id} は一覧に存在しますが詳細情報を取得できませんでした（一時的な取得失敗の可能性）。")

        return works_list

    def get_novel_text(self, novel_id: str) -> dict:
        """小説の本文テキスト、シリーズ情報、挿絵URLなどを取得します。"""
        url = f"https://www.pixiv.net/ajax/novel/{novel_id}"
        data = self._request_with_retry(url)
        body = data.get('body', {})
        
        return {
            'id': body.get('id'),
            'title': body.get('title'),
            'content': body.get('content', ''),
            'seriesNavData': body.get('seriesNavData'),
            'textEmbeddedImages': body.get('textEmbeddedImages', {}),
            'coverUrl': body.get('coverUrl', '')
        }

    def get_work_info(self, work_id: str) -> dict:
        """指定した作品IDの詳細情報を取得します。"""
        url = f"https://www.pixiv.net/ajax/illust/{work_id}"
        data = self._request_with_retry(url)
        body = data.get('body', {})
        if not body:
            return None
            
        return {
            'id': body.get('id'),
            'title': body.get('title'),
            'type': body.get('illustType', 0),
            'user_name': body.get('userName', 'Unknown'),
            'user_id': body.get('userId'),
            'page_count': body.get('pageCount', 1),
            'create_date': body.get('createDate', ''),
            'update_date': body.get('updateDate', '')
        }

    def get_novel_info(self, novel_id: str) -> dict:
        """指定した小説IDの詳細情報を取得します。"""
        url = f"https://www.pixiv.net/ajax/novel/{novel_id}"
        data = self._request_with_retry(url)
        body = data.get('body', {})
        if not body:
            return None
            
        return {
            'id': body.get('id'),
            'title': body.get('title'),
            'type': 'novel',
            'user_name': body.get('userName', 'Unknown'),
            'user_id': body.get('userId'),
            'text_count': body.get('textCount', 0),
            'create_date': body.get('createDate', ''),
            'update_date': body.get('updateDate', '')
        }

    def get_my_user_id(self) -> str:
        """ログイン中の自分のユーザーIDを取得します。"""
        url = "https://www.pixiv.net/"
        response = self.session.get(url, timeout=10)
        response.raise_for_status()
        
        # HTML内からuserIdを検索
        match = re.search(r'"userId"\s*:\s*"(\d+)"', response.text)
        if match:
            return match.group(1)
        
        # 別のフォーマットの可能性（JSONやスクリプト内）
        match_alt = re.search(r'pixiv\.context\.userId\s*=\s*"(\d+)"', response.text)
        if match_alt:
            return match_alt.group(1)
            
        raise Exception("ログイン中のユーザーIDが取得できませんでした。Cookieが有効か確認してください。")

    def get_following_users(self, my_user_id: str, rest_type: str = "show", log_callback=None) -> List[Dict[str, Any]]:
        """自分がフォローしているユーザー一覧を取得します。rest_type='show'（公開）または'hide'（非公開）"""
        following_list = []
        offset = 0
        limit = 24
        
        def _log(msg):
            logger.info(msg)
            if log_callback:
                log_callback(msg)
        
        _log(f"フォロー中のユーザー一覧（{rest_type}）の取得を開始します...")
        
        while True:
            url = f"https://www.pixiv.net/ajax/user/{my_user_id}/following"
            params = {
                'offset': offset,
                'limit': limit,
                'rest': rest_type,
                'lang': 'ja'
            }
            
            data = self._request_with_retry(url, params=params)
            body = data.get('body', {})
            users = body.get('users', [])
            
            if not users:
                break
                
            for user in users:
                following_list.append({
                    'user_id': str(user.get('userId')),
                    'name': user.get('userName'),
                    'account': user.get('userAccount'),
                    'profile_img': user.get('profileImageUrl')
                })
                
            _log(f"進捗: {len(following_list)} 人の作者情報を取得しました...")
            offset += limit
                
        return following_list

    def get_bookmarked_works(self, my_user_id: str, rest_type: str = "show", log_callback=None) -> List[Dict[str, Any]]:
        """自分がブックマークしているイラスト・マンガ一覧を取得します。"""
        works_list = []
        offset = 0
        limit = 48
        
        def _log(msg):
            logger.info(msg)
            if log_callback:
                log_callback(msg)
        
        _log(f"ブックマーク（イラスト・マンガ:{rest_type}）の取得を開始します...")
        
        seen_ids = set()
        
        while True:
            url = f"https://www.pixiv.net/ajax/user/{my_user_id}/illusts/bookmarks"
            params = {
                'tag': '',
                'offset': offset,
                'limit': limit,
                'rest': rest_type,
                'lang': 'ja'
            }
            
            data = self._request_with_retry(url, params=params)
            body = data.get('body', {})
            works = body.get('works', [])
            
            if not works:
                break
                
            new_works = 0
            for work in works:
                wid = str(work.get('id'))
                if wid in seen_ids:
                    continue
                seen_ids.add(wid)
                new_works += 1
                works_list.append({
                    'id': str(work.get('id')),
                    'title': work.get('title'),
                    'type': work.get('illustType', 0),
                    'user_name': work.get('userName', 'Unknown'),
                    'user_id': str(work.get('userId')),
                    'page_count': work.get('pageCount', 1),
                    'create_date': work.get('createDate', ''),
                    'update_date': work.get('updateDate', ''),
                    'thumb_url': work.get('url', ''),
                    'profile_img_url': work.get('profileImageUrl', '')
                })
                
            if new_works == 0:
                _log("新しいブックマークが見つからなくなったため取得を終了します。")
                break
                
            _log(f"進捗: {len(works_list)} 件のブックマーク情報を取得しました...")
            offset += limit
                
        return works_list

    def get_bookmarked_novels(self, my_user_id: str, rest_type: str = "show", log_callback=None) -> List[Dict[str, Any]]:
        """自分がブックマークしている小説一覧を取得します。"""
        works_list = []
        offset = 0
        limit = 48
        
        def _log(msg):
            logger.info(msg)
            if log_callback:
                log_callback(msg)
        
        _log(f"ブックマーク（小説:{rest_type}）の取得を開始します...")
        
        seen_ids = set()
        
        while True:
            url = f"https://www.pixiv.net/ajax/user/{my_user_id}/novels/bookmarks"
            params = {
                'tag': '',
                'offset': offset,
                'limit': limit,
                'rest': rest_type,
                'lang': 'ja'
            }
            
            data = self._request_with_retry(url, params=params)
            body = data.get('body', {})
            works = body.get('works', [])
            
            if not works:
                break
                
            new_works = 0
            for work in works:
                wid = str(work.get('id'))
                if wid in seen_ids:
                    continue
                seen_ids.add(wid)
                new_works += 1
                works_list.append({
                    'id': str(work.get('id')),
                    'title': work.get('title'),
                    'type': 'novel',
                    'user_name': work.get('userName', 'Unknown'),
                    'user_id': str(work.get('userId')),
                    'text_count': work.get('textCount', 0),
                    'create_date': work.get('createDate', ''),
                    'update_date': work.get('updateDate', ''),
                    'thumb_url': work.get('url', ''),
                    'profile_img_url': work.get('profileImageUrl', '')
                })
                
            if new_works == 0:
                _log("新しい小説ブックマークが見つからなくなったため取得を終了します。")
                break
                
            _log(f"進捗: {len(works_list)} 件の小説ブックマーク情報を取得しました...")
            offset += limit
                
        return works_list

    def get_image_urls(self, work_id: str) -> List[str]:
        """作品IDからオリジナル画像のURLリストを取得します。"""
        url = f"https://www.pixiv.net/ajax/illust/{work_id}/pages"
        data = self._request_with_retry(url)
        
        pages = data.get('body', [])
        urls = []
        for page in pages:
            original_url = page.get('urls', {}).get('original')
            if original_url:
                urls.append(original_url)
                
        return urls

    def download_image(self, url: str, save_path: str):
        """画像のURLからデータをダウンロードし、ローカルに保存します。"""
        import os
        interval, max_retries, retry_wait = self.get_rate_settings()
        tmp_path = save_path + ".tmp"
        for attempt in range(max_retries):
            try:
                sleep_time = random.uniform(interval * 0.8, interval * 1.2) if interval > 0 else 0
                if sleep_time > 0:
                    logger.debug(f"画像ダウンロード前に {sleep_time:.2f} 秒待機します。")
                    time.sleep(sleep_time)

                headers = {'Referer': 'https://www.pixiv.net/'}
                
                response = self.session.get(url, headers=headers, stream=True, timeout=15)
                response.raise_for_status()
                
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                
                with open(tmp_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                os.replace(tmp_path, save_path)
                
                logger.info(f"画像の保存に成功しました: {os.path.basename(save_path)}")
                # ダウンロード成功時にバックグラウンドでサムネイルを作成してキャッシュ保存
                threading.Thread(target=self.create_and_cache_thumbnail, args=(save_path,), daemon=True).start()
                return

            except (requests.RequestException, ValueError, Exception) as e:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                        
                logger.warning(f"画像のダウンロードに失敗しました（{attempt + 1}/{max_retries}回目）: {e}")
                if attempt == max_retries - 1:
                    logger.error("最大リトライ回数に達しました。")
                    raise
                
                is_429 = (isinstance(e, requests.HTTPError) and getattr(e, 'response', None) is not None and e.response.status_code == 429)
                if is_429:
                    logger.warning(f"429 Too Many Requests 検知: {retry_wait} 秒待機してから再試行します。")
                    time.sleep(retry_wait)
                else:
                    backoff_time = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(backoff_time)

    def get_csrf_token(self):
        try:
            url = "https://www.pixiv.net/"
            res = self.session.get(url, timeout=10)
            res.raise_for_status()
            
            import re
            import json
            
            # Method 1: direct JSON token pattern (most reliable on current Pixiv)
            match = re.search(r'"token"\s*:\s*"([a-f0-9]{32,64})"', res.text)
            if match:
                return match.group(1)

            # Method 2: meta-global-data
            match = re.search(r'id="meta-global-data" content=[\'"]([^\'"]+)[\'"]', res.text)
            if match:
                data_str = match.group(1).replace('&quot;', '"')
                try:
                    data = json.loads(data_str)
                    token = data.get('token')
                    if token:
                        return token
                except:
                    pass
                    
            # Method 3: meta name="csrf-token"
            match = re.search(r'<meta name="csrf-token" content="([^"]+)">', res.text)
            if match:
                return match.group(1)
                
            return None
        except Exception as e:
            logger.error(f"CSRFトークンの取得に失敗しました: {e}")
            return None

    def follow_user(self, target_user_id: str, restrict: int = 0) -> bool:
        """
        restrict: 0 = 公開フォロー, 1 = 非公開フォロー
        """
        token = self.get_csrf_token()
        if not token:
            raise Exception("CSRFトークンを取得できませんでした。")
            
        url = "https://www.pixiv.net/bookmark_add.php"
        headers = {
            'x-csrf-token': token,
            'origin': 'https://www.pixiv.net',
            'referer': f'https://www.pixiv.net/users/{target_user_id}'
        }
        data = {
            'mode': 'add',
            'type': 'user',
            'user_id': target_user_id,
            'restrict': restrict,
            'format': 'json'
        }
        
        try:
            res = self.session.post(url, headers=headers, data=data, timeout=10)
            res.raise_for_status()
            json_res = res.json()
            if isinstance(json_res, dict) and json_res.get('error'):
                raise Exception(json_res.get('message', '不明なエラー'))
            return True
        except Exception as e:
            logger.error(f"フォローに失敗しました: {e}")
            raise Exception(f"フォローAPI呼び出しエラー: {e}")
            
    def download_thumbnail_to_memory(self, url: str) -> bytes:
        """サムネイル画像用。インメモリ＋ローカルディスクの自動キャッシュ付き"""
        if not url:
            return None
        if url in self._memory_cache:
            return self._memory_cache[url]

        import hashlib
        import os
        url_hash = hashlib.md5(url.encode('utf-8')).hexdigest() + ".jpg"
        cache_file = os.path.join(self._cache_dir, url_hash)

        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'rb') as f:
                    data = f.read()
                if data:
                    self._memory_cache[url] = data
                    return data
            except Exception:
                pass

        for attempt in range(3):
            try:
                res = self.session.get(url, timeout=10)
                res.raise_for_status()
                data = res.content
                if data:
                    self._memory_cache[url] = data
                    try:
                        with open(cache_file, 'wb') as f:
                            f.write(data)
                    except Exception:
                        pass
                return data
            except Exception as e:
                if attempt == 2:
                    logger.error(f"サムネイル画像の取得に失敗しました: {e}")
                    return None
                time.sleep(1)

    def create_and_cache_thumbnail(self, image_path: str, size=(300, 300)) -> bytes:
        """ローカル画像ファイルからサムネイルを生成し、キャッシュディレクトリに保存してバイトデータを返します。"""
        import os
        if not image_path or not os.path.exists(image_path):
            return None

        path_hash = hashlib.md5((os.path.abspath(image_path) + f"_{size[0]}x{size[1]}").encode('utf-8')).hexdigest() + "_local.jpg"
        cache_file = os.path.join(self._cache_dir, path_hash)

        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'rb') as f:
                    data = f.read()
                if data:
                    return data
            except Exception:
                pass

        try:
            from PIL import Image
            import io
            with Image.open(image_path) as img:
                img.thumbnail(size)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80)
                data = buf.getvalue()
                try:
                    with open(cache_file, 'wb') as f:
                        f.write(data)
                except Exception:
                    pass
                return data
        except Exception as e:
            logger.debug(f"サムネイル生成エラー ({image_path}): {e}")
            return None

    def get_thumbnail_base64_from_path(self, image_path: str, size=(300, 300)) -> str:
        """ローカル画像ファイルからサムネイルのBase64文字列を取得します。"""
        import base64
        data = self.create_and_cache_thumbnail(image_path, size)
        if data:
            return base64.b64encode(data).decode('utf-8')
        return None
