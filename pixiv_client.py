import time
import random
import logging
import re
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
            status: "valid" | "warning" | "expired" | "missing"
            message: 表示用メッセージ
            expires_at: 最も早いセッションCookieの有効期限日時 (ISO string または None)
            user_id: ログイン中ユーザーID (取得できた場合)
        """
        import os
        import http.cookiejar
        from datetime import datetime
        
        if not os.path.exists(cookie_file):
            return {
                "status": "missing",
                "message": "cookies.txt が見つかりません。設定からインポートしてください。",
                "expires_at": None,
                "user_id": None
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
                    "user_id": None
                }
            
            min_expires = None
            for c in pixiv_cookies:
                if c.name == 'PHPSESSID' or (c.expires and c.expires > 0):
                    if c.expires and c.expires > 0:
                        if min_expires is None or c.expires < min_expires:
                            min_expires = c.expires
            
            now_ts = time.time()
            expires_iso = datetime.fromtimestamp(min_expires).isoformat() if min_expires else None
            
            if min_expires and min_expires < now_ts:
                return {
                    "status": "expired",
                    "message": "Cookieの有効期限が切れています。再インポートしてください。",
                    "expires_at": expires_iso,
                    "user_id": None
                }
            
            client = cls()
            user_id = client.get_my_user_id()
            if not user_id:
                return {
                    "status": "expired",
                    "message": "ログインセッションが無効化されています。再インポートしてください。",
                    "expires_at": expires_iso,
                    "user_id": None
                }
            
            if min_expires and (min_expires - now_ts) < 86400 * 7:
                days_left = max(1, int((min_expires - now_ts) / 86400))
                return {
                    "status": "warning",
                    "message": f"Cookie期限切れ間近 (残り約{days_left}日) - ID: {user_id}",
                    "expires_at": expires_iso,
                    "user_id": str(user_id)
                }
            
            return {
                "status": "valid",
                "message": f"ログイン中 - ユーザーID: {user_id}",
                "expires_at": expires_iso,
                "user_id": str(user_id)
            }
        except Exception as e:
            return {
                "status": "expired",
                "message": f"Cookie検証失敗または期限切れ ({str(e)})",
                "expires_at": None,
                "user_id": None
            }

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
