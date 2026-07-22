import os
import sys
import shutil
import flet as ft
import threading
import logging
from datetime import datetime
from pixiv_client import PixivClient
from database import Database
from core import run_backup, export_data, run_batch_backup, download_bookmarks, get_unfollowed_bookmark_authors, run_single_work_backup
import registry_helper
import base64
import time

logger = logging.getLogger(__name__)


class _EtaTimer:
    """残り時間を1秒ごとにカウントダウン表示するためのタイマー。
    core.py のコールバックが届くたびに eta_sec を補正し、
    独立したデーモンスレッドが毎秒 r_text を更新する。"""

    def __init__(self, r_text: "ft.Text", page: "ft.Page", update_lock: "threading.Lock"):
        self._r_text = r_text
        self._page = page
        # page.update() はスレッドセーフではなく、値の代入中や page.update() 実行中
        # （コントロールツリー走査中）に別スレッドが同じコントロールへ値を書き込むと
        # "Frozen controls cannot be updated" になり得るため、呼び出し元と同じロックを
        # 共有し、値の代入から page.update() までを不可分な区間として直列化する。
        self._update_lock = update_lock
        self._eta_sec: float = -1.0   # -1 = まだ計算できていない
        self._running = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # コールバックから ETA を更新（race-free）
    def update_eta(self, eta_sec: float):
        with self._lock:
            self._eta_sec = max(0.0, eta_sec)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            time.sleep(1)
            if not self._running:
                break
            with self._lock:
                eta = self._eta_sec
                if eta >= 0:
                    self._eta_sec = max(0.0, eta - 1)

            # 値の代入と page.update() は不可分（他スレッドの page.update() が走査中に
            # ここで値を書き換えると "Frozen controls cannot be updated" になり得るため）。
            # ロック外で value を書き換えないこと。
            try:
                with self._update_lock:
                    if eta < 0:
                        self._r_text.value = "残り時間計算中..."
                    elif eta == 0:
                        self._r_text.value = ""
                    else:
                        mins, secs = divmod(int(eta), 60)
                        hrs, mins = divmod(mins, 60)
                        if hrs > 0:
                            self._r_text.value = f"残り約{hrs}時間{mins}分{secs}秒"
                        elif mins > 0:
                            self._r_text.value = f"残り約{mins}分{secs}秒"
                        else:
                            self._r_text.value = f"残り約{secs}秒"
                    self._page.update()
            except Exception:
                pass

gui_log_callback = [None]
gui_queue_log_callback = [None]
gui_trigger_cookie_check = [None]
is_downloading_active = [False]
request_stop_all = [None]
# server.py（拡張機能連携）からもダウンロード中フラグを正しく管理できるようにするためのフック。
# gui.py 内の GUI 発のフローと合算して is_downloading_active を管理する。
gui_set_flow_active = [None]
# server.py（iOS版連携）が認証済みアクセスを検知するたびに呼ぶフック。
# 引数は time.time() のタイムスタンプ。ヘッダの接続インジケータ表示に使う。
gui_set_app_connected = [None]


def main_window(page: ft.Page, db: Database = None, scheduler=None):
    page.title = "PixivVault"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(color_scheme_seed="#0096FA")
    page.dark_theme = ft.Theme(color_scheme_seed="#0096FA")

    if db is None:
        db = Database()

    def _notify_scheduler():
        if scheduler and hasattr(scheduler, "on_settings_changed"):
            scheduler.on_settings_changed()

    # page.update() はスレッドセーフではないため、複数のバックグラウンドスレッド
    # （ダウンロード処理・_EtaTimer 等）から同時に呼ぶと "Frozen controls cannot be
    # updated" が発生し得る。すべての page.update() 呼び出しをこのロックで直列化する。
    _ui_update_lock = threading.Lock()

    def _safe_page_update():
        with _ui_update_lock:
            page.update()

    def _safe_update(control):
        with _ui_update_lock:
            control.update()

    def get_adjusted_color(col: str):
        if page.theme_mode == ft.ThemeMode.LIGHT:
            if col in [ft.Colors.BLUE_200, ft.Colors.BLUE_300, ft.Colors.BLUE_400]:
                return ft.Colors.BLUE_700
            elif col in [ft.Colors.GREEN_300, ft.Colors.GREEN_400]:
                return ft.Colors.GREEN_700
            elif col in [ft.Colors.RED_300, ft.Colors.RED_400, ft.Colors.ORANGE_300, ft.Colors.ORANGE_400]:
                return ft.Colors.ERROR
            elif col in [ft.Colors.GREY_300, ft.Colors.GREY_400, ft.Colors.GREY_500]:
                return ft.Colors.ON_SURFACE_VARIANT
        else:
            if col in [ft.Colors.ORANGE_300, ft.Colors.ORANGE_400]:
                return ft.Colors.ERROR
            elif col in [ft.Colors.GREY_600, ft.Colors.GREY_700, ft.Colors.GREY_800]:
                return ft.Colors.GREY_400
        return col

    # --- 共通コントロール ---
    # 個別DL・一括DL・ブックマークDLは互いに独立して同時起動できるため、
    # stop/pause イベントを共有すると一方の停止操作がもう一方に影響してしまう。
    # フローごとに専用の Event を持たせ、実行中フロー数を数えて is_downloading_active を管理する。
    single_stop_event  = threading.Event()
    single_pause_event = threading.Event()
    batch_stop_event   = threading.Event()
    batch_pause_event  = threading.Event()
    bm_stop_event       = threading.Event()
    bm_pause_event      = threading.Event()

    _active_flows = set()

    def _set_flow_active(name: str, active: bool):
        if active:
            _active_flows.add(name)
        else:
            _active_flows.discard(name)
        is_downloading_active[0] = bool(_active_flows)

    def _stop_all_flows():
        single_stop_event.set()
        batch_stop_event.set()
        bm_stop_event.set()

    request_stop_all[0] = _stop_all_flows
    gui_set_flow_active[0] = _set_flow_active

    log_area = ft.ListView(expand=True, spacing=2, auto_scroll=True)
    log_container = ft.Container(
        content=log_area,
        border=ft.Border(
            top=ft.BorderSide(1, ft.Colors.OUTLINE), bottom=ft.BorderSide(1, ft.Colors.OUTLINE),
            left=ft.BorderSide(1, ft.Colors.OUTLINE), right=ft.BorderSide(1, ft.Colors.OUTLINE),
        ),
        border_radius=5, padding=10, height=140, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST
    )

    list_expanded = [False]
    def toggle_list_expansion(e=None):
        list_expanded[0] = not list_expanded[0]
        with _ui_update_lock:
            log_container.visible = not list_expanded[0]
            page.update()

    def append_log(msg: str, color: str = ft.Colors.ON_SURFACE):
        time_str = datetime.now().strftime("%y%m%d %H:%M:%S")
        final_color = get_adjusted_color(color)
        with _ui_update_lock:
            log_area.controls.append(ft.Text(f"[{time_str}] {msg}", color=final_color, selectable=True, size=13, weight=ft.FontWeight.W_500))
            # 1000件超過分は先頭から削除してメモリリークを防止する
            if len(log_area.controls) > 1000:
                del log_area.controls[0]
            page.update()
    def handle_log(msg: str, color: str = ft.Colors.ON_SURFACE):
        append_log(msg, color=color)
    def handle_alert(msg: str):
        append_log(f"[!] {msg}", color=ft.Colors.RED_400)

    gui_log_callback[0] = handle_log
    if scheduler:
        scheduler.log_callback = append_log

    login_status_text = ft.Text("ログインチェック中...", color=ft.Colors.PRIMARY, size=13, weight=ft.FontWeight.W_500)

    # ── iOS版 接続インジケータ ──────────────────────────────
    # server.py が認証済みアクセス(/ping・ダウンロード命令等)を検知するたびに
    # gui_set_app_connected 経由で最終接続時刻が届く。直近60秒以内なら「接続中」、
    # それ以外は最終接続からの経過時間を表示する。10秒ごとのティッカーで自然に
    # 「未接続」へ減衰させる（HTTPはステートレスなので明示的な切断通知は無い）。
    _last_app_contact_holder = [None]
    app_connection_icon = ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.ON_SURFACE_VARIANT, size=10)
    app_connection_text = ft.Text("アプリ: 未接続", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

    def _refresh_app_connection_label():
        # トグルOFF中は「最終接続からの経過時間」に関係なく、常に「無効」表示を優先する。
        # OFFにした直後でも直近60秒以内の接続履歴が残っていると「接続中」のまま見えて
        # しまう（トグルは新規接続を拒否するだけで過去の接続履歴は消さない仕様）ため、
        # トグル操作に応じて即座に切り替わるようにする。
        if db.get_setting('web_bridge_enabled', '1') != '1':
            icon_color, label = ft.Colors.RED_400, "アプリ: 切断中"
        else:
            last = _last_app_contact_holder[0]
            if last is None:
                icon_color, label = ft.Colors.ON_SURFACE_VARIANT, "アプリ: 未接続"
            else:
                elapsed = time.time() - last
                if elapsed < 60:
                    icon_color, label = ft.Colors.GREEN_400, "アプリ: 接続中"
                else:
                    mins = int(elapsed // 60)
                    icon_color = ft.Colors.ON_SURFACE_VARIANT
                    label = f"アプリ: 未接続（最終接続 {mins}分前）" if mins > 0 else "アプリ: 未接続（最終接続 1分未満前）"
        try:
            with _ui_update_lock:
                app_connection_icon.color = icon_color
                app_connection_text.value = label
                app_connection_text.color = icon_color
                page.update()
        except Exception:
            pass

    def _on_app_connected(timestamp):
        _last_app_contact_holder[0] = timestamp
        _refresh_app_connection_label()

    gui_set_app_connected[0] = _on_app_connected

    def _app_connection_ticker():
        while True:
            time.sleep(10)
            _refresh_app_connection_label()

    threading.Thread(target=_app_connection_ticker, daemon=True).start()

    # サーバー自体は落とさず、iOS版からの接続受け付けだけをGUIから
    # 手動でON/OFFできるようにするトグル（拡張機能連携には影響しない）。
    # 実機テスト時に「アプリ未接続」状態を意図的に作れるようにするためのもの。
    def _on_web_bridge_toggle_change(e):
        db.set_setting('web_bridge_enabled', '1' if e.control.value else '0')
        _refresh_app_connection_label()

    web_bridge_toggle = ft.Switch(
        value=db.get_setting('web_bridge_enabled', '1') == '1',
        on_change=_on_web_bridge_toggle_change,
        active_color=ft.Colors.GREEN_400,
        scale=0.8,
    )

    def clear_user_id_field(e):
        with _ui_update_lock:
            user_id_field.value = ""
            user_id_field.update()

    user_id_field = ft.TextField(
        label="PixivユーザーID",
        width=240,
        suffix=ft.IconButton(
            icon=ft.Icons.CLEAR,
            icon_size=16,
            tooltip="入力を削除",
            on_click=clear_user_id_field
        )
    )
    mode_dropdown = ft.Dropdown(
        label="実行モード", width=150,
        options=[
            ft.DropdownOption(key="diff", text="差分DL"),
            ft.DropdownOption(key="full", text="完全DL"),
        ],
        value="diff"
    )
    target_type_dropdown = ft.Dropdown(
        label="対象", width=160,
        options=[
            ft.DropdownOption(key="illust", text="イラスト・漫画"),
            ft.DropdownOption(key="novel", text="小説"),
            ft.DropdownOption(key="both", text="両方"),
        ],
        value="both"
    )
    run_btn    = ft.ElevatedButton("実行", icon=ft.Icons.PLAY_ARROW)
    pause_btn  = ft.ElevatedButton("一時停止", icon=ft.Icons.PAUSE, disabled=True)
    stop_btn   = ft.ElevatedButton("停止", icon=ft.Icons.STOP, disabled=True)
    export_btn = ft.ElevatedButton("エクスポート", icon=ft.Icons.ARCHIVE)

    progress_bar  = ft.ProgressBar(width=400, value=0, visible=False)
    progress_text = ft.Text("0 / 0", visible=False)
    remaining_time_text = ft.Text("", size=13, color=ft.Colors.PRIMARY, weight=ft.FontWeight.W_500)

    progress_history = []
    # 個別DL / ブックマークDL ごとに独立した EtaTimer を持つ（同時実行時の混線防止）
    _single_eta_timer: _EtaTimer | None = None
    _bm_eta_timer:     _EtaTimer | None = None

    def _calc_eta(history: list, current: int, total: int, elapsed_sec: float) -> float:
        """直近の進捗履歴からETA（秒）を計算して返す。計算不能なら -1.0。"""
        if elapsed_sec <= 0 or current >= total:
            return -1.0
        history.append((current, elapsed_sec))
        if len(history) > 10:
            history.pop(0)
        if len(history) >= 2:
            items_done = history[-1][0] - history[0][0]
            time_taken = history[-1][1] - history[0][1]
            if time_taken > 0 and items_done > 0:
                speed = items_done / time_taken
                return (total - current) / speed
        return -1.0

    def handle_progress(current: int, total: int, elapsed_sec: float = 0,
                        p_bar=progress_bar, p_text=progress_text, r_text=remaining_time_text,
                        history=None, eta_timer_ref: list | None = None):
        """個別DL・ブックマークDL 共用の進捗コールバック。
        eta_timer_ref には [_EtaTimer インスタンスまたは None] を格納したリストを渡す。
        """
        nonlocal _single_eta_timer, _bm_eta_timer
        if history is None:
            history = progress_history
        # eta_timer_ref 未指定時は個別DLタイマーを使う
        if eta_timer_ref is None:
            eta_timer_ref = [_single_eta_timer]

        eta = _calc_eta(history, current, total, elapsed_sec)
        if eta >= 0 and eta_timer_ref[0] is not None:
            eta_timer_ref[0].update_eta(eta)

        # 値の代入から page.update() までを _EtaTimer と同じロックで不可分に行う
        # （他スレッドの page.update() 走査中に値を書き換えると frozen エラーになるため）
        with _ui_update_lock:
            p_bar.value = current / total if total > 0 else 0
            p_text.value = f"{current} / {total}"

            if current == total:
                r_text.value = ""
                history.clear()

            page.update()

    def set_ui_disabled_single(disabled: bool, is_running: bool = False):
        with _ui_update_lock:
            user_id_field.disabled = disabled
            mode_dropdown.disabled = disabled
            target_type_dropdown.disabled = disabled
            run_btn.disabled       = disabled
            export_btn.disabled    = disabled
            pause_btn.disabled     = not is_running
            stop_btn.disabled      = not is_running
            if not is_running:
                pause_btn.text = "一時停止"
                pause_btn.icon = ft.Icons.PAUSE
            page.update()

    def run_backup_thread():
        nonlocal _single_eta_timer
        user_id = user_id_field.value.strip()
        if not user_id:
            append_log("ユーザーIDを入力してください。", color=ft.Colors.ERROR)
            with _ui_update_lock:
                run_btn.disabled = False
                page.update()
            return
        if not user_id.isdigit():
            append_log("ユーザーIDは数字のみ入力してください (PixivのユーザーページURLの末尾の数字です)。", color=ft.Colors.ERROR)
            with _ui_update_lock:
                run_btn.disabled = False
                page.update()
            return
        if not os.path.exists("cookies.txt"):
            handle_alert("cookies.txt が見つかりません。設定ボタンからインポートしてください。")
            with _ui_update_lock:
                run_btn.disabled = False
                page.update()
            return

        is_full = (mode_dropdown.value == "full")
        target_type = target_type_dropdown.value
        append_log("--- 個別ダウンロードを開始します ---", color=ft.Colors.BLUE_300)
        with _ui_update_lock:
            progress_bar.value = 0
            progress_bar.visible = True
            progress_text.visible = True
            page.update()
        single_stop_event.clear()
        single_pause_event.clear()
        set_ui_disabled_single(True, is_running=True)
        _set_flow_active("single", True)
        _single_eta_timer = _EtaTimer(remaining_time_text, page, update_lock=_ui_update_lock)
        _single_eta_timer.start()

        try:
            client = PixivClient(db=db)
            run_backup(
                user_id=user_id, client=client, db=db, is_full=is_full, target_type=target_type,
                log_callback=handle_log, progress_callback=handle_progress,
                alert_callback=handle_alert, stop_event=single_stop_event, pause_event=single_pause_event
            )
            append_log("--- 個別ダウンロードが完了しました ---", color=ft.Colors.GREEN_400)
        except Exception as e:
            if single_stop_event.is_set():
                handle_alert("処理が中止されました。")
            else:
                handle_alert(f"エラー: {e}")
        finally:
            if _single_eta_timer is not None:
                _single_eta_timer.stop()
                _single_eta_timer = None
            with _ui_update_lock:
                progress_bar.visible = False
                progress_text.visible = False
                remaining_time_text.value = ""
                page.update()
            progress_history.clear()
            _set_flow_active("single", False)
            set_ui_disabled_single(False, is_running=False)

    def on_run_click(e):
        # 連打を防ぐため、スレッド起動前に即座にボタンを無効化する
        with _ui_update_lock:
            run_btn.disabled = True
            page.update()
        threading.Thread(target=run_backup_thread, daemon=True).start()
    run_btn.on_click = on_run_click

    tab1_content = ft.Column([
        ft.Row([user_id_field, mode_dropdown, target_type_dropdown, run_btn, pause_btn, stop_btn, export_btn], wrap=True),
        ft.Row([progress_bar, progress_text, remaining_time_text]),
    ])

    # --- タブ2: フォロー中一括ダウンロードUI ---
    follow_list_view = ft.ListView(expand=True, spacing=5)
    follow_checkboxes = {}
    
    follow_count_text = ft.Text("0", size=10, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)
    follow_count_badge = ft.Container(
        content=follow_count_text,
        bgcolor=ft.Colors.BLUE_700,
        border_radius=10,
        padding=ft.padding.Padding(5, 2, 5, 2),
        margin=ft.margin.Margin(left=-10, top=5, right=0, bottom=0)
    )

    batch_summary_card = None  # 後の行で実体を代入する

    def hide_batch_summary(e=None):
        if batch_summary_card is not None and batch_summary_card.visible:
            with _ui_update_lock:
                batch_summary_card.visible = False
                page.update()

    batch_target_type_dropdown = ft.Dropdown(
        label="対象", width=160,
        options=[
            ft.DropdownOption(key="illust", text="イラスト・漫画"),
            ft.DropdownOption(key="novel", text="小説"),
            ft.DropdownOption(key="both", text="両方"),
        ],
        value="both"
    )
    batch_target_type_dropdown.on_change = lambda e: hide_batch_summary()


    def clear_search_field(e):
        hide_batch_summary()
        with _ui_update_lock:
            search_field.value = ""
            search_field.update()
        load_follow_list_ui(search_val_override="")

    search_field = ft.TextField(
        label="名前・IDで検索",
        width=190,
        height=40,
        content_padding=10,
        suffix=ft.IconButton(
            icon=ft.Icons.CLEAR,
            icon_size=16,
            tooltip="検索内容を削除",
            on_click=clear_search_field
        )
    )

    sort_by_dropdown = ft.Dropdown(
        label="並び替え",
        width=150,
        options=[
            ft.DropdownOption(key="follow_order", text="フォロー登録順"),
            ft.DropdownOption(key="name", text="名前"),
        ],
        value="follow_order"
    )
    sort_order_dropdown = ft.Dropdown(
        label="順序",
        width=110,
        options=[
            ft.DropdownOption(key="asc", text="昇順"),
            ft.DropdownOption(key="desc", text="降順"),
        ],
        value="asc"
    )
    sort_by_dropdown.on_select    = lambda e: (hide_batch_summary(), load_follow_list_ui())
    sort_order_dropdown.on_select = lambda e: (hide_batch_summary(), load_follow_list_ui())

    batch_run_btn    = ft.ElevatedButton("一括ダウンロード実行", icon=ft.Icons.PLAY_ARROW)
    batch_pause_btn  = ft.ElevatedButton("一時停止", icon=ft.Icons.PAUSE, disabled=True)
    batch_stop_btn   = ft.ElevatedButton("停止", icon=ft.Icons.STOP, disabled=True)
    select_all_btn   = ft.TextButton("すべて選択")
    deselect_all_btn = ft.TextButton("選択解除")
    select_favorite_btn = ft.TextButton("お気に入りのみ選択")

    batch_progress_bar  = ft.ProgressBar(width=400, value=0, visible=False)
    batch_progress_text = ft.Text("0 / 0", visible=False)
    batch_remaining_time_text = ft.Text("", size=13, color=ft.Colors.PRIMARY, weight=ft.FontWeight.W_500)

    def load_follow_list_ui(search_val_override=None):
        # 現在のチェックボックスの選択状態を退避
        saved_states = {uid: cb.value for uid, cb in follow_checkboxes.items()}

        users = db.get_following_users(sort_by=sort_by_dropdown.value, sort_order=sort_order_dropdown.value)

        # 検索値: 引数優先、なければフィールドから取得
        search_q = search_val_override if search_val_override is not None else (search_field.value or "")
        search_q = search_q.strip().lower()
        if search_q:
            users = [u for u in users if search_q in (u.get('name') or '').lower() or search_q in str(u.get('user_id', '')).lower()]

        def toggle_zip(e, uid):
            btn = e.control
            is_zipped = btn.icon == ft.Icons.ARCHIVE
            new_val = not is_zipped
            db.set_zipped(uid, new_val)
            with _ui_update_lock:
                btn.icon = ft.Icons.ARCHIVE if new_val else ft.Icons.ARCHIVE_OUTLINED
                btn.icon_color = ft.Colors.BLUE_400 if new_val else ft.Colors.GREY_500
                page.update()

        def toggle_favorite(e, uid):
            btn = e.control
            is_fav = btn.icon == ft.Icons.STAR
            new_val = not is_fav
            db.set_favorite(uid, new_val)
            with _ui_update_lock:
                btn.icon = ft.Icons.STAR if new_val else ft.Icons.STAR_BORDER
                btn.icon_color = ft.Colors.YELLOW_600 if new_val else ft.Colors.GREY_500
                page.update()

        new_rows = []
        new_checkboxes = {}
        for u in users:
            label = f"{u['name']} (ID:{u['user_id']})"
            if u.get('last_downloaded'):
                label += f" [最終: {u['last_downloaded'][:10]}]"

            # 退避しておいた選択状態を復元（デフォルトは False）
            cb = ft.Checkbox(
                value=saved_states.get(u['user_id'], False),
                on_change=lambda e: hide_batch_summary()
            )
            new_checkboxes[u['user_id']] = cb

            def on_label_tap(e, cb_ref=cb):
                hide_batch_summary()
                with _ui_update_lock:
                    cb_ref.value = not cb_ref.value
                    page.update()
                
            gd_content = ft.Container(
                content=ft.Text(label),
                padding=10,
                alignment=ft.alignment.Alignment.CENTER_LEFT,
            )
            gd = ft.GestureDetector(
                content=gd_content,
                on_tap=on_label_tap,
                on_double_tap=toggle_list_expansion,
                mouse_cursor=ft.MouseCursor.CLICK,
                expand=True
            )
            
            is_zipped = u.get('is_zipped', 0)
            zip_btn = ft.IconButton(
                icon=ft.Icons.ARCHIVE if is_zipped else ft.Icons.ARCHIVE_OUTLINED,
                icon_color=ft.Colors.BLUE_400 if is_zipped else ft.Colors.GREY_500,
                tooltip="個別Zip化",
                on_click=lambda e, uid=u['user_id']: toggle_zip(e, uid)
            )

            is_favorite = u.get('is_favorite', 0)
            fav_btn = ft.IconButton(
                icon=ft.Icons.STAR if is_favorite else ft.Icons.STAR_BORDER,
                icon_color=ft.Colors.YELLOW_600 if is_favorite else ft.Colors.GREY_500,
                tooltip="お気に入り (新着自動チェック)",
                on_click=lambda e, uid=u['user_id']: toggle_favorite(e, uid)
            )
            
            def show_author_settings_dialog(e, uid, uname):
                cur_target = db.get_author_setting(uid, "target_type", "default")
                cur_zip = db.get_author_setting(uid, "auto_archive", "default")

                dd_target = ft.Dropdown(
                    label="ダウンロード対象のタイプ",
                    value=cur_target,
                    options=[
                        ft.DropdownOption("default", "デフォルトに従う"),
                        ft.DropdownOption("both", "すべて (イラスト+小説)"),
                        ft.DropdownOption("illust", "イラストのみ"),
                        ft.DropdownOption("novel", "小説のみ"),
                    ],
                    width=320
                )
                dd_zip = ft.Dropdown(
                    label="ダウンロード後のZIP化",
                    value=cur_zip,
                    options=[
                        ft.DropdownOption("default", "デフォルトに従う"),
                        ft.DropdownOption("1", "常に有効 (ZIP化する)"),
                        ft.DropdownOption("0", "常に無効 (ZIP化しない)"),
                    ],
                    width=320
                )

                def on_save_author_setting(ev):
                    if dd_target.value == "default":
                        db.delete_author_setting(uid, "target_type")
                    else:
                        db.set_author_setting(uid, "target_type", dd_target.value)
                    
                    if dd_zip.value == "default":
                        db.delete_author_setting(uid, "auto_archive")
                    else:
                        db.set_author_setting(uid, "auto_archive", dd_zip.value)
                    page.pop_dialog()
                    append_log(f"「{uname}」様の個別ダウンロード設定を保存しました。", color=ft.Colors.GREEN_400)
                    load_follow_list_ui()

                dlg = ft.AlertDialog(
                    title=ft.Text(f"「{uname}」個別設定", size=16, weight=ft.FontWeight.BOLD),
                    content=ft.Column([
                        ft.Text("この作者のみ適用するオーバーライド設定です。", size=13, color=ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.W_500),
                        dd_target,
                        dd_zip
                    ], tight=True, spacing=10),
                    actions=[
                        ft.TextButton("キャンセル", on_click=lambda _: page.pop_dialog()),
                        ft.ElevatedButton("保存する", on_click=on_save_author_setting)
                    ]
                )
                page.show_dialog(dlg)

            override_btn = ft.IconButton(
                icon=ft.Icons.TUNE,
                icon_color=ft.Colors.TEAL_300 if db.get_all_author_settings(u['user_id']) else ft.Colors.GREY_500,
                tooltip="作者ごとのダウンロード設定",
                on_click=lambda e, uid=u['user_id'], uname=u['name']: show_author_settings_dialog(e, uid, uname)
            )

            row = ft.Row([cb, fav_btn, gd, override_btn, zip_btn], key=str(u['user_id']))
            new_rows.append(row)

        # follow_checkboxes/follow_list_view/follow_count_text への反映と
        # ページ更新をひとつのロック区間にまとめ、他スレッドの page.update() と
        # 競合して "Frozen controls cannot be updated" にならないようにする。
        with _ui_update_lock:
            follow_checkboxes.clear()
            follow_checkboxes.update(new_checkboxes)
            follow_list_view.controls.clear()
            follow_list_view.controls.extend(new_rows)
            follow_count_text.value = str(len(users))
            follow_list_view.update()
            page.update()
        
    search_field.on_change  = lambda e: (hide_batch_summary(), load_follow_list_ui(search_val_override=e.control.value))

    def set_ui_disabled_batch(disabled: bool, is_running: bool = False):
        with _ui_update_lock:
            batch_run_btn.disabled    = disabled
            batch_target_type_dropdown.disabled = disabled
            select_all_btn.disabled   = disabled
            deselect_all_btn.disabled = disabled
            select_favorite_btn.disabled = disabled
            for cb in follow_checkboxes.values():
                cb.disabled = disabled
            batch_pause_btn.disabled = not is_running
            batch_stop_btn.disabled  = not is_running
            if not is_running:
                batch_pause_btn.text = "一時停止"
                batch_pause_btn.icon = ft.Icons.PAUSE
            page.update()

    batch_progress_history = []
    _batch_eta_timer: _EtaTimer | None = None

    def handle_batch_progress(idx: int, total: int, user_id: str, elapsed_sec: float = 0):
        nonlocal _batch_eta_timer
        eta = _calc_eta(batch_progress_history, idx, total, elapsed_sec)
        if eta >= 0 and _batch_eta_timer is not None:
            _batch_eta_timer.update_eta(eta)

        with _ui_update_lock:
            batch_progress_bar.value  = idx / total if total > 0 else 0
            batch_progress_text.value = f"作者 {idx} / {total}"

            if idx == total:
                batch_remaining_time_text.value = ""
                batch_progress_history.clear()

            page.update()

    batch_summary_text = ft.Row(spacing=10, wrap=True)
    batch_summary_card = ft.Container(
        content=ft.Row([
            ft.Row([
                ft.Icon(ft.Icons.ANALYTICS, color=ft.Colors.BLUE_400),
                ft.Text("実行結果サマリー: ", weight=ft.FontWeight.BOLD),
                batch_summary_text
            ], spacing=10, wrap=True, expand=True),
            ft.IconButton(
                icon=ft.Icons.CLOSE,
                icon_size=18,
                tooltip="サマリーを閉じる",
                on_click=hide_batch_summary
            )
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        padding=10,
        border_radius=8,
        border=ft.Border(
            top=ft.BorderSide(1, ft.Colors.BLUE_400), bottom=ft.BorderSide(1, ft.Colors.BLUE_400),
            left=ft.BorderSide(1, ft.Colors.BLUE_400), right=ft.BorderSide(1, ft.Colors.BLUE_400)
        ),
        visible=False
    )

    def run_batch_thread():
        nonlocal _batch_eta_timer
        selected_ids = [uid for uid, cb in follow_checkboxes.items() if cb.value]
        if not selected_ids:
            append_log("ダウンロードする作者を選択してください。", color=ft.Colors.ERROR)
            with _ui_update_lock:
                batch_run_btn.disabled = False
                page.update()
            return
        if not os.path.exists("cookies.txt"):
            handle_alert("cookies.txt が見つかりません。")
            with _ui_update_lock:
                batch_run_btn.disabled = False
                page.update()
            return

        append_log(f"--- {len(selected_ids)}人の一括ダウンロードを開始します ---", color=ft.Colors.BLUE_300)
        with _ui_update_lock:
            batch_progress_bar.value   = 0
            batch_progress_bar.visible = True
            batch_progress_text.visible = True
            page.update()
        batch_stop_event.clear()
        batch_pause_event.clear()
        set_ui_disabled_batch(True, is_running=True)
        _set_flow_active("batch", True)
        _batch_eta_timer = _EtaTimer(batch_remaining_time_text, page, update_lock=_ui_update_lock)
        _batch_eta_timer.start()

        try:
            client = PixivClient(db=db)
            target_type = batch_target_type_dropdown.value
            with _ui_update_lock:
                batch_summary_card.visible = False
                page.update()
            stats = run_batch_backup(
                user_ids=selected_ids, client=client, db=db, is_full=False, target_type=target_type,
                log_callback=handle_log, progress_callback=None, alert_callback=handle_alert,
                stop_event=batch_stop_event, pause_event=batch_pause_event, batch_progress_callback=handle_batch_progress
            )
            if stats and isinstance(stats, dict):
                with _ui_update_lock:
                    batch_summary_text.controls = [
                        ft.Container(content=ft.Text(f"〇 新規: {stats.get('new_count', 0)}件", color=ft.Colors.GREEN, weight=ft.FontWeight.BOLD), bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.GREEN), padding=4, border_radius=4),
                        ft.Container(content=ft.Text(f"△ 更新: {stats.get('updated_count', 0)}件", color=ft.Colors.BLUE, weight=ft.FontWeight.BOLD), bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.BLUE), padding=4, border_radius=4),
                        ft.Container(content=ft.Text(f"× 削除検知: {stats.get('deleted_count', 0)}件", color=ft.Colors.RED if stats.get('deleted_count', 0) > 0 else ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.BOLD), bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.RED) if stats.get('deleted_count', 0) > 0 else ft.Colors.SURFACE_CONTAINER_HIGHEST, padding=4, border_radius=4),
                        ft.Container(content=ft.Text(f"↺ 復帰: {stats.get('restored_count', 0)}件", color=ft.Colors.TEAL, weight=ft.FontWeight.BOLD), bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.TEAL), padding=4, border_radius=4),
                        ft.Text(f"スキップ: {stats.get('skipped_count', 0)}件 | エラー: {stats.get('failed_count', 0)}件", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
                    ]
                    batch_summary_card.visible = True
                    page.update()
            append_log("--- 一括ダウンロードが完了しました ---", color=ft.Colors.GREEN_400)
        except Exception as e:
            if batch_stop_event.is_set():
                handle_alert("一括処理が中止されました。")
            else:
                handle_alert(f"エラー: {e}")
        finally:
            if _batch_eta_timer is not None:
                _batch_eta_timer.stop()
                _batch_eta_timer = None
            with _ui_update_lock:
                batch_progress_bar.visible  = False
                batch_progress_text.visible = False
                batch_remaining_time_text.value = ""
                page.update()
            batch_progress_history.clear()
            _set_flow_active("batch", False)
            set_ui_disabled_batch(False, is_running=False)
            load_follow_list_ui()

    def on_batch_run_click(e):
        # 連打を防ぐため、スレッド起動前に即座にボタンを無効化する
        with _ui_update_lock:
            batch_run_btn.disabled = True
            page.update()
        threading.Thread(target=run_batch_thread, daemon=True).start()
    batch_run_btn.on_click = on_batch_run_click

    def on_batch_pause(e):
        with _ui_update_lock:
            if batch_pause_event.is_set():
                batch_pause_event.clear()
                batch_pause_btn.text = "一時停止"
                batch_pause_btn.icon = ft.Icons.PAUSE
            else:
                batch_pause_event.set()
                batch_pause_btn.text = "再開"
                batch_pause_btn.icon = ft.Icons.PLAY_ARROW
            page.update()

    batch_pause_btn.on_click = on_batch_pause
    def _on_batch_stop(e):
        batch_stop_event.set()
        batch_pause_event.set()  # 一時停止中でも即座にスレッドを起こす
    batch_stop_btn.on_click  = _on_batch_stop

    def on_select_all(e):
        hide_batch_summary()
        with _ui_update_lock:
            for cb in follow_checkboxes.values():
                cb.value = True
            page.update()

    def on_deselect_all(e):
        hide_batch_summary()
        with _ui_update_lock:
            for cb in follow_checkboxes.values():
                cb.value = False
            page.update()

    select_all_btn.on_click   = on_select_all
    deselect_all_btn.on_click = on_deselect_all

    def on_select_favorite(e):
        hide_batch_summary()
        favs = [str(u['user_id']) for u in db.get_favorite_users()]
        with _ui_update_lock:
            for uid, cb in follow_checkboxes.items():
                cb.value = str(uid) in favs
            page.update()
    select_favorite_btn.on_click = on_select_favorite

    batch_actions_row = ft.Row([
        select_all_btn,
        deselect_all_btn,
        select_favorite_btn,
        ft.VerticalDivider(),
        batch_target_type_dropdown,
        search_field,
        sort_by_dropdown,
        sort_order_dropdown,
        batch_run_btn,
        batch_pause_btn,
        batch_stop_btn
    ], wrap=True)

    tab2_content = ft.Column([
        batch_actions_row,
        ft.Row([batch_progress_bar, batch_progress_text, batch_remaining_time_text]),
        batch_summary_card,
        ft.Container(
            content=follow_list_view, expand=True,
            border=ft.Border(
                top=ft.BorderSide(1, ft.Colors.OUTLINE), bottom=ft.BorderSide(1, ft.Colors.OUTLINE),
                left=ft.BorderSide(1, ft.Colors.OUTLINE), right=ft.BorderSide(1, ft.Colors.OUTLINE)
            ),
            padding=5, border_radius=5
        )
    ], expand=True)

    # タブ1 共通イベント
    def on_pause_click(e):
        with _ui_update_lock:
            if single_pause_event.is_set():
                single_pause_event.clear()
                pause_btn.text = "一時停止"
                pause_btn.icon = ft.Icons.PAUSE
            else:
                single_pause_event.set()
                pause_btn.text = "再開"
                pause_btn.icon = ft.Icons.PLAY_ARROW
            page.update()
    pause_btn.on_click = on_pause_click
    def _on_single_stop(e):
        single_stop_event.set()
        single_pause_event.set()  # 一時停止中でも即座にスレッドを起こす
    stop_btn.on_click  = _on_single_stop

    def run_export_thread():
        append_log("--- エクスポートを開始します ---", color=ft.Colors.BLUE_300)
        try:
            export_data(db=db, log_callback=handle_log)
            append_log("--- エクスポートが完了しました ---", color=ft.Colors.GREEN_400)
        except Exception as e:
            handle_alert(f"エラー: {e}")
    export_btn.on_click = lambda _: threading.Thread(target=run_export_thread, daemon=True).start()

    # --- 設定ダイアログ ---
    cookie_status_text = ft.Text("", selectable=True, visible=False, size=13, weight=ft.FontWeight.W_500)
    sync_status_text   = ft.Text("", selectable=True, visible=False, size=13, weight=ft.FontWeight.W_500)
    save_path_text     = ft.Text(
        f"現在の保存先: {db.get_setting('save_path', 'Images')}",
        selectable=True, color=ft.Colors.PRIMARY, size=13, weight=ft.FontWeight.W_600
    )

    # --- ファイル/フォルダー選択 (tkinterのネイティブダイアログを使用) ---
    def _pick_file_macos(title: str):
        """macOS用のファイル選択(osascript経由)。
        tkinterはバックグラウンドスレッドからtk.Tk()を呼ぶとCocoaの制約でクラッシュするため、
        別プロセスとして起動され、どのスレッドから呼んでも安全なosascriptの'choose file'を使う。
        """
        import subprocess
        script = f'POSIX path of (choose file with prompt "{title}")'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _pick_folder_macos(title: str):
        """macOS用のフォルダー選択(osascript経由)。理由は_pick_file_macosと同じ。"""
        import subprocess
        script = f'POSIX path of (choose folder with prompt "{title}")'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _run_cookie_file_picker():
        """cookies.txt のファイル選択ダイアログをバックグラウンドスレッドで実行"""
        import sys
        try:
            if sys.platform == "darwin":
                file_path = _pick_file_macos("cookies.txt を選択")
            else:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.wm_attributes("-topmost", True)
                file_path = filedialog.askopenfilename(
                    parent=root,
                    title="cookies.txt を選択",
                    filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
                )
                root.destroy()
            if file_path:
                dst_path = os.path.abspath("cookies.txt")
                src_path = os.path.abspath(file_path)
                if src_path != dst_path:
                    shutil.copy2(src_path, dst_path)
                with _ui_update_lock:
                    cookie_status_text.value = "[完了] cookies.txt をインポートしました。"
                    cookie_status_text.color = get_adjusted_color(ft.Colors.GREEN_400)
                    cookie_status_text.visible = True
                    page.update()
                threading.Thread(target=lambda: check_login_status(on_cookie_imported=True), daemon=True).start()
            else:
                with _ui_update_lock:
                    cookie_status_text.value = "キャンセルされました。"
                    cookie_status_text.color = get_adjusted_color(ft.Colors.GREY_400)
                    cookie_status_text.visible = True
                    page.update()
        except Exception as ex:
            with _ui_update_lock:
                cookie_status_text.value = f"[失敗] {ex}"
                cookie_status_text.color = ft.Colors.RED_400
                cookie_status_text.visible = True
                page.update()

    def _run_folder_picker():
        """保存先フォルダー選択ダイアログをバックグラウンドスレッドで実行"""
        import sys
        try:
            if sys.platform == "darwin":
                folder_path = _pick_folder_macos("画像の保存先フォルダーを選択")
            else:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.wm_attributes("-topmost", True)
                folder_path = filedialog.askdirectory(
                    parent=root,
                    title="画像の保存先フォルダーを選択"
                )
                root.destroy()
            if folder_path:
                db.set_setting("save_path", folder_path)
                with _ui_update_lock:
                    save_path_text.value = f"現在の保存先: {folder_path}"
                    page.update()
                append_log(f"保存先フォルダーを {folder_path} に変更しました。")
            else:
                append_log("フォルダー選択がキャンセルされました。")
        except Exception as ex:
            append_log(f"フォルダー選択エラー: {ex}", color=ft.Colors.RED_400)

    def sync_follow_list():
        with _ui_update_lock:
            sync_status_text.value = "Pixivから同期中..."
            sync_status_text.color = get_adjusted_color(ft.Colors.BLUE_400)
            sync_status_text.visible = True
            page.update()
        try:
            client = PixivClient(db=db)
            my_id  = client.get_my_user_id()
            users  = client.get_following_users(my_user_id=my_id, rest_type="show", log_callback=handle_log)
            users.extend(client.get_following_users(my_user_id=my_id, rest_type="hide", log_callback=handle_log))
            db.save_following_users(users)
            db.set_setting("last_sync_date", datetime.now().isoformat())
            with _ui_update_lock:
                sync_status_text.value = f"[完了] 同期完了 ({len(users)}人の作者)"
                sync_status_text.color = get_adjusted_color(ft.Colors.GREEN_400)
                sync_status_text.visible = True
                page.update()
            load_follow_list_ui()
        except Exception as e:
            with _ui_update_lock:
                sync_status_text.value = f"[失敗] 同期失敗: {e}"
                sync_status_text.color = ft.Colors.RED_400
                sync_status_text.visible = True
                page.update()

    def on_zip_all_change(e):
        db.set_setting("zip_all_after_download", "1" if e.control.value else "0")

    zip_all_checkbox = ft.Checkbox(
        label="すべての作者に対しダウンロード完了後zipにする",
        value=db.get_setting("zip_all_after_download", "0") == "1",
        on_change=on_zip_all_change
    )
    
    def on_novel_format_change(e):
        db.set_setting("novel_save_format", e.control.value)
        
    novel_format_dropdown = ft.Dropdown(
        label="小説の保存形式",
        options=[
            ft.DropdownOption("epub", "EPUBのみ (推奨)"),
            ft.DropdownOption("txt", "TXTのみ"),
            ft.DropdownOption("both", "EPUBとTXT両方"),
        ],
        value=db.get_setting("novel_save_format", "epub"),
        width=430
    )
    novel_format_dropdown.on_change = on_novel_format_change

    def on_ugoira_format_change(e):
        db.set_setting("ugoira_save_format", e.control.value)

    ugoira_format_dropdown = ft.Dropdown(
        label="うごイラの保存形式",
        options=[
            ft.DropdownOption("gif", "GIF動画 (デフォルト)"),
            ft.DropdownOption("mp4", "MP4動画"),
            ft.DropdownOption("folder", "すべてフォルダ格納する (ZIP解凍+JSON)"),
        ],
        value=db.get_setting("ugoira_save_format", "gif"),
        width=430
    )
    ugoira_format_dropdown.on_change = on_ugoira_format_change

    def on_auto_check_interval_change(e):
        db.set_setting("auto_check_interval_hours", e.control.value)
        _notify_scheduler()

    auto_check_dropdown = ft.Dropdown(
        label="☆ お気に入り自動チェック間隔",
        options=[
            ft.DropdownOption("6", "6時間ごと"),
            ft.DropdownOption("12", "12時間ごと"),
            ft.DropdownOption("24", "24時間ごと (1日)"),
            ft.DropdownOption("48", "48時間ごと (2日)"),
            ft.DropdownOption("0", "自動チェックしない"),
        ],
        value=db.get_setting("auto_check_interval_hours", "24"),
        width=430
    )
    auto_check_dropdown.on_change = on_auto_check_interval_change

    def on_check_on_startup_change(e):
        db.set_setting("check_on_startup", "1" if e.control.value else "0")
        _notify_scheduler()

    check_on_startup_checkbox = ft.Checkbox(
        label="起動時にもお気に入り新着チェックを実行する",
        value=db.get_setting("check_on_startup", "0") == "1",
        on_change=on_check_on_startup_change
    )

    def on_enable_notifications_change(e):
        db.set_setting("enable_notifications", e.control.value)

    notifications_dropdown = ft.Dropdown(
        label="新着通知のON/OFF",
        options=[
            ft.DropdownOption("1", "ON (通知する)"),
            ft.DropdownOption("0", "OFF (通知しない)"),
        ],
        value=db.get_setting("enable_notifications", "1"),
        width=430
    )
    notifications_dropdown.on_change = on_enable_notifications_change

    def on_bookmark_download_mode_change(e):
        db.set_setting("bookmark_download_mode", e.control.value)

    bookmark_download_mode_dropdown = ft.Dropdown(
        label="ブックマーク保存モード",
        options=[
            ft.DropdownOption(key="direct", text="ブックマークフォルダ内は作者別フォルダを作らず直下にフラット配置"),
            ft.DropdownOption(key="link",   text="ブックマークフォルダ内も作者別フォルダを作成して階層化配置"),
        ],
        value=db.get_setting("bookmark_download_mode", "direct"),
        width=430
    )
    bookmark_download_mode_dropdown.on_change = on_bookmark_download_mode_change

    
    minimize_to_tray_checkbox = ft.Checkbox(
        label="最小化時にタスクトレイに格納する",
        value=db.get_setting("minimize_to_tray", "1") == "1",
        on_change=lambda e: db.set_setting("minimize_to_tray", "1" if e.control.value else "0")
    )
    
    cache_status_text = ft.Text("", size=13, weight=ft.FontWeight.W_500)

    def on_clear_cache_click(e):
        import os
        cache_dir = os.path.join(os.getcwd(), "cache")
        deleted_count = 0
        if os.path.exists(cache_dir):
            for root, dirs, files in os.walk(cache_dir):
                for f in files:
                    try:
                        os.remove(os.path.join(root, f))
                        deleted_count += 1
                    except Exception:
                        pass
        with _ui_update_lock:
            if deleted_count > 0:
                cache_status_text.value = f"キャッシュを削除しました ({deleted_count}件)"
                cache_status_text.color = get_adjusted_color(ft.Colors.GREEN_400)
            else:
                cache_status_text.value = "キャッシュはありませんでした。"
                cache_status_text.color = get_adjusted_color(ft.Colors.GREY_400)
            page.update()
        append_log(f"画像のキャッシュを削除しました ({deleted_count}件)")

    clear_cache_btn = ft.ElevatedButton(
        "キャッシュを削除する",
        icon=ft.Icons.DELETE_OUTLINE,
        on_click=on_clear_cache_click
    )

    clear_cache_container = ft.Container(
        content=ft.Column([
            ft.Text("未フォロー作者確認時のサムネイル画像キャッシュを削除します。", size=13, color=ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.W_500),
            ft.Row([clear_cache_btn, cache_status_text], vertical_alignment=ft.CrossAxisAlignment.CENTER)
        ], spacing=4),
        padding=ft.padding.Padding(10, 6, 0, 6)
    )

    def on_download_interval_change(e):
        db.set_setting("download_interval", e.control.value)

    download_interval_dropdown = ft.Dropdown(
        label="ダウンロード待機時間 (秒)",
        options=[
            ft.DropdownOption("0.5", "0.5秒 (高速)"),
            ft.DropdownOption("1.0", "1.0秒 (標準)"),
            ft.DropdownOption("1.5", "1.5秒 (推奨)"),
            ft.DropdownOption("3.0", "3.0秒 (安全)"),
            ft.DropdownOption("5.0", "5.0秒 (慎重)"),
        ],
        value=db.get_setting("download_interval", "1.5"),
        width=430
    )
    download_interval_dropdown.on_change = on_download_interval_change

    def on_api_retry_count_change(e):
        db.set_setting("api_retry_count", e.control.value)

    api_retry_count_dropdown = ft.Dropdown(
        label="通信エラー時リトライ上限回数",
        options=[
            ft.DropdownOption("1", "1回"),
            ft.DropdownOption("3", "3回 (推奨)"),
            ft.DropdownOption("5", "5回"),
            ft.DropdownOption("10", "10回"),
        ],
        value=db.get_setting("api_retry_count", "3"),
        width=430
    )
    api_retry_count_dropdown.on_change = on_api_retry_count_change

    def on_api_retry_wait_change(e):
        db.set_setting("api_retry_wait", e.control.value)

    api_retry_wait_dropdown = ft.Dropdown(
        label="429 レート制限検知時待機時間",
        options=[
            ft.DropdownOption("3.0", "3秒"),
            ft.DropdownOption("5.0", "5秒 (推奨)"),
            ft.DropdownOption("10.0", "10秒"),
            ft.DropdownOption("30.0", "30秒 (確実リカバリー)"),
        ],
        value=db.get_setting("api_retry_wait", "5.0"),
        width=430
    )
    api_retry_wait_dropdown.on_change = on_api_retry_wait_change

    advanced_settings = ft.ExpansionTile(
        title=ft.Text("5. Advanced / 高度な設定", weight=ft.FontWeight.BOLD),
        controls=[
            ft.Container(
                content=ft.Column([
                    download_interval_dropdown,
                    api_retry_count_dropdown,
                    api_retry_wait_dropdown,
                    zip_all_checkbox,
                    minimize_to_tray_checkbox,
                    clear_cache_container
                ], spacing=12),
                padding=ft.padding.Padding(14, 14, 0, 8)
            )
        ]
    )

    def on_settings_close(e=None):
        db.set_setting("check_on_startup", "1" if check_on_startup_checkbox.value else "0")
        if auto_check_dropdown.value is not None:
            db.set_setting("auto_check_interval_hours", str(auto_check_dropdown.value))
        if novel_format_dropdown.value is not None:
            db.set_setting("novel_save_format", str(novel_format_dropdown.value))
        if ugoira_format_dropdown.value is not None:
            db.set_setting("ugoira_save_format", str(ugoira_format_dropdown.value))
        if notifications_dropdown.value is not None:
            db.set_setting("enable_notifications", str(notifications_dropdown.value))
        if bookmark_download_mode_dropdown.value is not None:
            db.set_setting("bookmark_download_mode", str(bookmark_download_mode_dropdown.value))
        if download_interval_dropdown.value is not None:
            db.set_setting("download_interval", str(download_interval_dropdown.value))
        if api_retry_count_dropdown.value is not None:
            db.set_setting("api_retry_count", str(api_retry_count_dropdown.value))
        if api_retry_wait_dropdown.value is not None:
            db.set_setting("api_retry_wait", str(api_retry_wait_dropdown.value))
        db.set_setting("zip_all_after_download", "1" if zip_all_checkbox.value else "0")
        db.set_setting("minimize_to_tray", "1" if minimize_to_tray_checkbox.value else "0")
        _notify_scheduler()
        page.pop_dialog()

    settings_dialog = ft.AlertDialog(
        title=ft.Text("設定"),
        content=ft.Column([
            ft.Text("1. Cookie のインポート", weight=ft.FontWeight.BOLD),
            ft.Text("Pixivのログイン状態を引き継ぐための cookies.txt を選択してください。", size=13, color=ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.W_500),
            ft.ElevatedButton("ファイルを選択", icon=ft.Icons.UPLOAD_FILE,
                              on_click=lambda _: threading.Thread(target=_run_cookie_file_picker, daemon=True).start()),
            cookie_status_text,
            ft.Divider(),
            ft.Text("2. フォロー中リストの同期", weight=ft.FontWeight.BOLD),
            ft.Text("Pixivからフォロー中の作者情報を取得し、DBに保存します。", size=13, color=ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.W_500),
            ft.ElevatedButton("リストを同期", icon=ft.Icons.SYNC,
                              on_click=lambda _: threading.Thread(target=sync_follow_list, daemon=True).start()),
            sync_status_text,
            ft.Divider(),
            ft.Text("3. 保存フォルダーの設定", weight=ft.FontWeight.BOLD),
            ft.Text("画像のダウンロード先フォルダーを指定します（再起動後も保持）。", size=13, color=ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.W_500),
            ft.ElevatedButton("フォルダーを選択", icon=ft.Icons.FOLDER_OPEN,
                              on_click=lambda _: threading.Thread(target=_run_folder_picker, daemon=True).start()),
            save_path_text,
            ft.Divider(),
            ft.Text("4. その他の設定", weight=ft.FontWeight.BOLD),
            check_on_startup_checkbox,
            auto_check_dropdown,
            novel_format_dropdown,
            ugoira_format_dropdown,
            notifications_dropdown,
            bookmark_download_mode_dropdown,
            ft.Divider(),
            advanced_settings,

            ft.Row([
                ft.Text("v1.0.0 build 260719", size=11, color=ft.Colors.GREY_600)
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Row([
                ft.TextButton("閉じる", on_click=on_settings_close)
            ], alignment=ft.MainAxisAlignment.END),
        ], tight=True, width=500, height=420, scroll=ft.ScrollMode.AUTO, spacing=8),
    )

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

    def open_author_folder(folder_path):
        import subprocess
        import sys
        if not folder_path or not os.path.exists(folder_path):
            page.show_dialog(ft.SnackBar(ft.Text("対象の保存フォルダが見つかりません。"), bgcolor=ft.Colors.RED_700))
            _safe_page_update()
            return
        try:
            if sys.platform == "win32":
                os.startfile(folder_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder_path])
            else:
                subprocess.Popen(["xdg-open", folder_path])
        except Exception as e:
            logger.error(f"保存フォルダを開けませんでした: {e}")

    history_btn = ft.IconButton(
        icon=ft.Icons.HISTORY, tooltip="直近のバックアップ",
        on_click=lambda _: on_tab_change(6)
    )

    open_folder_btn = ft.IconButton(
        icon=ft.Icons.FOLDER_OPEN, tooltip="保存フォルダを開く",
        on_click=open_save_folder
    )

    def toggle_theme(e):
        with _ui_update_lock:
            if page.theme_mode == ft.ThemeMode.DARK:
                page.theme_mode = ft.ThemeMode.LIGHT
                theme_toggle_btn.icon = ft.Icons.DARK_MODE
                theme_toggle_btn.tooltip = "ダークモードへ切り替え"
            else:
                page.theme_mode = ft.ThemeMode.DARK
                theme_toggle_btn.icon = ft.Icons.LIGHT_MODE
                theme_toggle_btn.tooltip = "ライトモードへ切り替え"

            for btn in tab_buttons:
                if btn.data != 99:
                    is_selected = (btn.data == current_tab_idx[0])
                    btn.style = ft.ButtonStyle(
                        color=ft.Colors.PRIMARY if is_selected else ft.Colors.ON_SURFACE,
                        bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.PRIMARY) if is_selected else ft.Colors.TRANSPARENT,
                    )

            for ctrl in log_area.controls:
                if hasattr(ctrl, "color") and ctrl.color:
                    ctrl.color = get_adjusted_color(ctrl.color)
            for ctrl in queue_log_area.controls:
                if hasattr(ctrl, "color") and ctrl.color:
                    ctrl.color = get_adjusted_color(ctrl.color)

            page.update()

        # update_ext_status() は内部で _ui_update_lock を取得するため、
        # 上のロック区間の外で呼び出す(同一スレッドでの二重取得によるデッドロックを避けるため)。
        update_ext_status()

    theme_toggle_btn = ft.IconButton(
        icon=ft.Icons.LIGHT_MODE, tooltip="ライトモードへ切り替え",
        on_click=toggle_theme
    )

    def open_settings_dialog(e=None):
        with _ui_update_lock:
            check_on_startup_checkbox.value = (db.get_setting("check_on_startup", "0") == "1")
            auto_check_dropdown.value = db.get_setting("auto_check_interval_hours", "24")
            novel_format_dropdown.value = db.get_setting("novel_save_format", "epub")
            ugoira_format_dropdown.value = db.get_setting("ugoira_save_format", "gif")
            notifications_dropdown.value = db.get_setting("enable_notifications", "1")
            bookmark_download_mode_dropdown.value = db.get_setting("bookmark_download_mode", "direct")
            download_interval_dropdown.value = db.get_setting("download_interval", "1.5")
            api_retry_count_dropdown.value = db.get_setting("api_retry_count", "3")
            api_retry_wait_dropdown.value = db.get_setting("api_retry_wait", "5.0")
            zip_all_checkbox.value = (db.get_setting("zip_all_after_download", "0") == "1")
            minimize_to_tray_checkbox.value = (db.get_setting("minimize_to_tray", "1") == "1")
            if cookie_status_text.color:
                cookie_status_text.color = get_adjusted_color(cookie_status_text.color)
            if sync_status_text.color:
                sync_status_text.color = get_adjusted_color(sync_status_text.color)
            if cache_status_text.color:
                cache_status_text.color = get_adjusted_color(cache_status_text.color)
            save_path_text.color = ft.Colors.PRIMARY
        page.show_dialog(settings_dialog)

    settings_btn = ft.IconButton(
        icon=ft.Icons.SETTINGS, tooltip="設定",
        on_click=open_settings_dialog
    )

    def open_pixiv_in_browser(e=None):
        import webbrowser
        webbrowser.open("https://www.pixiv.net/")
        append_log("ブラウザで Pixiv を開きました。PV拡張機能をお使いの場合、ページを開くと自動で Cookie が連携・更新されます。", color=ft.Colors.BLUE_300)

    cookie_picker_btn = ft.IconButton(
        icon=ft.Icons.PUBLIC,
        tooltip="ブラウザで Pixiv を開く (ログイン状態確認・自動連携)",
        on_click=open_pixiv_in_browser
    )

    def close_cookie_banner(e=None):
        with _ui_update_lock:
            cookie_banner.visible = False
            cookie_banner.update()

    cookie_banner = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.INFO_OUTLINE, color=ft.Colors.BLUE_400),
            ft.Column([
                ft.Text("【Cookie未連携】", weight=ft.FontWeight.BOLD, size=14, color=ft.Colors.ON_SURFACE),
                ft.Text("PV拡張機能をご利用中の場合、cookies.txtの手動DLは不要です。\nブラウザでPixivを開くだけで自動的に連携されます。", size=13, color=ft.Colors.ON_SURFACE_VARIANT),
            ], expand=True, spacing=2),
            ft.ElevatedButton("Pixivを開く", icon=ft.Icons.OPEN_IN_BROWSER, on_click=open_pixiv_in_browser),
            ft.IconButton(ft.Icons.CLOSE, tooltip="閉じる", on_click=close_cookie_banner),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=10,
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        border=ft.Border(
            top=ft.BorderSide(1, ft.Colors.BLUE_400), bottom=ft.BorderSide(1, ft.Colors.BLUE_400),
            left=ft.BorderSide(1, ft.Colors.BLUE_400), right=ft.BorderSide(1, ft.Colors.BLUE_400),
        ),
        border_radius=8,
        visible=False,
    )

    # --- 拡張機能タブ ---

    # 状態アイコン・テキスト（update_ext_status で書き換える）
    ext_status_icon    = ft.Icon(ft.Icons.HELP_OUTLINE, color=ft.Colors.ON_SURFACE_VARIANT, size=28)
    ext_status_label   = ft.Text("確認中...", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.ON_SURFACE_VARIANT)
    ext_status_detail  = ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT)

    # 登録・解除ボタン
    ext_register_btn   = ft.ElevatedButton(
        "有効化（レジストリ登録）", icon=ft.Icons.CHECK_CIRCLE_OUTLINE,
        bgcolor=ft.Colors.BLUE_600, color=ft.Colors.WHITE,
    )
    ext_unregister_btn = ft.OutlinedButton(
        "無効化（レジストリ解除）", icon=ft.Icons.REMOVE_CIRCLE_OUTLINE,
    )

    def update_ext_status():
        import sys
        with _ui_update_lock:
            if sys.platform != "win32":
                ext_status_icon.name    = ft.Icons.CHECK_CIRCLE
                ext_status_icon.color   = ft.Colors.GREEN_400
                ext_status_label.value  = "有効  ─  macOSのURLスキームで自動解決されます"
                ext_status_label.color  = ft.Colors.GREEN_400
                ext_status_detail.value = "追加の登録操作は不要です。ブラウザ拡張機能からPixivVaultを自動起動できます。"
                ext_register_btn.disabled   = True
                ext_unregister_btn.disabled = True
            else:
                is_registered = registry_helper.check_protocol_registered()
                if is_registered:
                    ext_status_icon.name    = ft.Icons.CHECK_CIRCLE
                    ext_status_icon.color   = ft.Colors.GREEN_400
                    ext_status_label.value  = "有効  ─  自動起動は登録済みです"
                    ext_status_label.color  = ft.Colors.GREEN_400
                    ext_status_detail.value = "ブラウザ拡張機能からPixivVaultを自動で起動できる状態です。"
                    ext_register_btn.disabled   = True
                    ext_unregister_btn.disabled = False
                else:
                    ext_status_icon.name    = ft.Icons.CANCEL
                    ext_status_icon.color   = ft.Colors.ON_SURFACE_VARIANT
                    ext_status_label.value  = "無効  ─  自動起動は未登録です"
                    ext_status_label.color  = ft.Colors.ON_SURFACE_VARIANT
                    ext_status_detail.value = "「有効化」ボタンを押すと、拡張機能からの自動起動が使えるようになります。"
                    ext_register_btn.disabled   = False
                    ext_unregister_btn.disabled = True
            page.update()

    def on_register_ext(e):
        if registry_helper.register_protocol():
            page.show_dialog(ft.SnackBar(ft.Text("自動起動を有効にしました！"), bgcolor=ft.Colors.GREEN_700))
        else:
            page.show_dialog(ft.SnackBar(ft.Text("登録に失敗しました。"), bgcolor=ft.Colors.RED_700))
        update_ext_status()

    def on_unregister_ext(e):
        if registry_helper.unregister_protocol():
            page.show_dialog(ft.SnackBar(ft.Text("自動起動を無効にしました。"), bgcolor=ft.Colors.GREY_700))
        else:
            page.show_dialog(ft.SnackBar(ft.Text("解除に失敗しました。"), bgcolor=ft.Colors.RED_700))
        update_ext_status()

    ext_register_btn.on_click   = on_register_ext
    ext_unregister_btn.on_click = on_unregister_ext

    # 状態カード
    ext_status_card = ft.Container(
        content=ft.Row([
            ext_status_icon,
            ft.Column([
                ext_status_label,
                ext_status_detail,
            ], spacing=2, expand=True),
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=16,
        border_radius=10,
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        border=ft.Border(
            top=ft.BorderSide(1, ft.Colors.OUTLINE),
            bottom=ft.BorderSide(1, ft.Colors.OUTLINE),
            left=ft.BorderSide(1, ft.Colors.OUTLINE),
            right=ft.BorderSide(1, ft.Colors.OUTLINE),
        ),
    )

    tab_extension_content = ft.Column([
        # ── 上部：タイトル ──
        ft.Text("ブラウザ拡張機能  連携設定", size=18, weight=ft.FontWeight.BOLD),
        ft.Divider(height=8),
        # ── 現在の状態カード ──
        ft.Text("現在の状態", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.ON_SURFACE_VARIANT),
        ext_status_card,
        # ── 操作ボタン ──
        ft.Row([ext_register_btn, ext_unregister_btn], spacing=12),
        ft.Divider(height=8),
        # ── 説明 ──
        ft.Text(
            "PixivVaultのブラウザ拡張機能を使うと、Pixivのページから直接ダウンロード指示を送ることができます。\n"
            "Cookie は拡張機能が30分ごとに自動で同期します。Pixivを開くと即時同期されます。",
            size=12, color=ft.Colors.ON_SURFACE_VARIANT
        ),
        # ── 注意事項（下部）──
        ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.WARNING_AMBER, color=ft.Colors.ERROR, size=16),
                    ft.Text("自動起動について（注意）", weight=ft.FontWeight.BOLD, color=ft.Colors.ERROR, size=12),
                ], spacing=4),
                ft.Text(
                    "有効にすると、Windowsレジストリ（HKEY_CURRENT_USER）にカスタムURLスキームを書き込みます。\n"
                    "アプリが未起動の状態で拡張機能ボタンを押した場合、自動でアプリを起動します。",
                    size=12, color=ft.Colors.ON_SURFACE_VARIANT
                ),
            ], spacing=4),
            bgcolor=ft.Colors.with_opacity(0.10, ft.Colors.ERROR),
            padding=10,
            border_radius=8,
        ),
    ], spacing=8)

    # 初期状態チェック
    update_ext_status()


    # --- タブ: キュー ---
    queue_log_area = ft.ListView(expand=True, spacing=2, auto_scroll=True)
    queue_log_container = ft.Container(
        content=queue_log_area,
        border=ft.Border(
            top=ft.BorderSide(1, ft.Colors.OUTLINE),
            bottom=ft.BorderSide(1, ft.Colors.OUTLINE),
            left=ft.BorderSide(1, ft.Colors.OUTLINE),
            right=ft.BorderSide(1, ft.Colors.OUTLINE),
        ),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        border_radius=5,
        padding=10,
        expand=True
    )

    def append_queue_log(msg: str, color: str = ft.Colors.ON_SURFACE):
        time_str = datetime.now().strftime("%y%m%d %H:%M:%S")
        final_color = get_adjusted_color(color)
        with _ui_update_lock:
            queue_log_area.controls.append(
                ft.Text(f"[{time_str}] {msg}", color=final_color, selectable=True, size=13, weight=ft.FontWeight.W_500)
            )
            page.update()

    gui_queue_log_callback[0] = append_queue_log

    tab_queue_content = ft.Column([
        ft.Row([
            ft.Icon(ft.Icons.LIST_ALT, color=ft.Colors.BLUE_400),
            ft.Text("拡張機能からのダウンロードキューログ", size=16, weight=ft.FontWeight.BOLD),
        ]),
        ft.Text("ブラウザ拡張機能から追加されたダウンロードリクエストの進捗をリアルタイム表示します。", size=13, color=ft.Colors.ON_SURFACE_VARIANT),
        queue_log_container
    ], expand=True)

    # --- タブ: ブックマーク ---
    bm_target_type_dropdown = ft.Dropdown(
        label="対象",
        options=[
            ft.DropdownOption("illust", "イラスト・マンガ"),
            ft.DropdownOption("novel", "小説"),
            ft.DropdownOption("both", "両方")
        ],
        value="both",
        width=150
    )
    bm_rest_type_dropdown = ft.Dropdown(
        label="公開範囲",
        options=[
            ft.DropdownOption("show", "公開"),
            ft.DropdownOption("hide", "非公開")
        ],
        value="show",
        width=150
    )
    bm_run_btn = ft.ElevatedButton("ブックマーク一括DL実行", icon=ft.Icons.PLAY_ARROW)
    bm_pause_btn = ft.ElevatedButton("一時停止", icon=ft.Icons.PAUSE, disabled=True)
    bm_stop_btn = ft.ElevatedButton("停止", icon=ft.Icons.STOP, disabled=True)
    
    bm_progress_bar = ft.ProgressBar(width=400, value=0, visible=False)
    bm_progress_text = ft.Text("0/0", visible=False)
    bm_remaining_time_text = ft.Text("", visible=True)
    bm_progress_history = []
    _bm_eta_timer_ref: list = [None]  # [_EtaTimer | None] ブックマークDL用

    def run_bookmark_download_thread():
        my_user_id = db.get_setting("my_user_id", "")
        if not my_user_id:
            handle_alert("ログインチェックを完了してください。")
            return

        with _ui_update_lock:
            bm_run_btn.disabled = True
            bm_pause_btn.disabled = False
            bm_stop_btn.disabled = False
            bm_progress_bar.value = 0
            bm_progress_bar.visible = True
            bm_progress_text.visible = True
            page.update()
        _set_flow_active("bookmark", True)
        bm_stop_event.clear()
        bm_pause_event.clear()
        bm_eta = _EtaTimer(bm_remaining_time_text, page, update_lock=_ui_update_lock)
        _bm_eta_timer_ref[0] = bm_eta
        bm_eta.start()

        try:
            client = PixivClient(db=db)
            download_bookmarks(
                db=db, client=client, my_user_id=my_user_id,
                target_type=bm_target_type_dropdown.value,
                rest_type=bm_rest_type_dropdown.value,
                log_callback=append_log,
                progress_callback=lambda c, t, e: handle_progress(
                    c, t, e, bm_progress_bar, bm_progress_text,
                    bm_remaining_time_text, bm_progress_history,
                    eta_timer_ref=_bm_eta_timer_ref
                ),
                alert_callback=handle_alert, stop_event=bm_stop_event, pause_event=bm_pause_event
            )
            append_log("--- ブックマーク一括DL完了 ---", color=ft.Colors.GREEN_400)
        except Exception as e:
            if bm_stop_event.is_set():
                handle_alert("処理が中止されました。")
            else:
                handle_alert(f"エラー: {e}")
        finally:
            if _bm_eta_timer_ref[0] is not None:
                _bm_eta_timer_ref[0].stop()
                _bm_eta_timer_ref[0] = None
            with _ui_update_lock:
                bm_run_btn.disabled = False
                bm_pause_btn.disabled = True
                bm_stop_btn.disabled = True
                bm_progress_bar.visible = False
                bm_progress_text.visible = False
                bm_remaining_time_text.value = ""
                page.update()
            bm_progress_history.clear()
            _set_flow_active("bookmark", False)

    bm_run_btn.on_click = lambda _: threading.Thread(target=run_bookmark_download_thread, daemon=True).start()
    def _on_bm_stop(e):
        bm_stop_event.set()
        bm_pause_event.set()  # 一時停止中でも即座にスレッドを起こす
    bm_stop_btn.on_click = _on_bm_stop

    def on_bm_pause(e):
        with _ui_update_lock:
            if bm_pause_event.is_set():
                bm_pause_event.clear()
                bm_pause_btn.text = "一時停止"
                bm_pause_btn.icon = ft.Icons.PAUSE
            else:
                bm_pause_event.set()
                bm_pause_btn.text = "再開"
                bm_pause_btn.icon = ft.Icons.PLAY_ARROW
            page.update()
    bm_pause_btn.on_click = on_bm_pause

    check_unfollowed_btn = ft.ElevatedButton("未フォロー作者を確認", icon="person_search")

    def run_check_unfollowed():
        # ダイアログを閉じる/開く操作のたびに専用のフラグを作ることで、
        # 「未フォロー作者を確認」を連続で開いた際に片方を閉じてももう片方の
        # サムネイル読み込みが止まらないようにする(モジュール共有のフラグは使わない)。
        session_active = [True]
        my_user_id = db.get_setting("my_user_id", "")
        if not my_user_id:
            handle_alert("ログインチェックを完了してください。")
            return

        with _ui_update_lock:
            check_unfollowed_btn.disabled = True
            page.update()

        try:
            client = PixivClient(db=db)
            authors = get_unfollowed_bookmark_authors(
                db, client, my_user_id,
                target_type=bm_target_type_dropdown.value,
                rest_type=bm_rest_type_dropdown.value,
                log_callback=append_log
            )
        except Exception as e:
            logger.exception(f"未フォロー作者の抽出に失敗しました: {e}")
            handle_alert(f"抽出エラー: {e}")
            with _ui_update_lock:
                check_unfollowed_btn.disabled = False
                page.update()
            return

        if not authors:
            handle_alert("未フォローの作者は見つかりませんでした。")
            with _ui_update_lock:
                check_unfollowed_btn.disabled = False
                page.update()
            return

        # ダイアログを最初に開く（中身は空）
        lv = ft.ListView(expand=True, spacing=5)
        loading_text = ft.Text(f"0 / {len(authors)} 件を読み込み中...")
        dialog = ft.AlertDialog(
            title=ft.Text(f"未フォローの作者 ({len(authors)}人)"),
            content=ft.Container(
                width=600,
                height=500,
                content=ft.Column([loading_text, lv], expand=True)
            ),
            actions=[ft.TextButton("閉じる", on_click=lambda _: close_dialog())]
        )

        def close_dialog():
            session_active[0] = False
            page.pop_dialog()

        page.show_dialog(dialog)
        with _ui_update_lock:
            check_unfollowed_btn.disabled = False
            page.update()

        # 作者リストを1件ずつ追加するスレッド（page.run_threadで管理）
        def load_authors():
            containers_to_load = []
            for i, author in enumerate(authors):
                if not session_active[0]:
                    break

                status_text = ft.Text("")
                follow_btn = ft.ElevatedButton("フォロー", icon="person_add")
                work_thumb_container = ft.Container(
                    width=80, height=80,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    border_radius=6
                )
                icon_container = ft.Container(
                    width=28, height=28,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    border_radius=14
                )

                # クロージャ用にauthor情報をコピー
                _author = dict(author)

                def make_on_follow(au, fb, st):
                    def on_follow(e):
                        with _ui_update_lock:
                            fb.disabled = True
                            page.update()
                        try:
                            client.follow_user(au['user_id'])
                            with db.lock:
                                db.conn.execute(
                                    "INSERT OR IGNORE INTO following_users (user_id, name, is_zipped) VALUES (?, ?, ?)",
                                    (au['user_id'], au['user_name'],
                                     1 if db.get_setting("auto_archive_new_users", "0") == "1" else 0)
                                )
                            with _ui_update_lock:
                                st.value = "フォロー済"
                                st.color = ft.Colors.GREEN_400
                                fb.visible = False
                                page.update()
                            append_log(f"フォローしました: {au['user_name']} ({au['user_id']})")
                            load_follow_list_ui()
                        except Exception as ex:
                            with _ui_update_lock:
                                st.value = "失敗"
                                st.color = ft.Colors.RED_400
                                fb.disabled = False
                                page.update()
                            append_log(f"フォロー失敗: {ex}")
                    return on_follow

                follow_btn.on_click = make_on_follow(_author, follow_btn, status_text)

                row = ft.Row([
                    work_thumb_container,
                    ft.Column([
                        ft.Row([
                            icon_container,
                            ft.Text(f"{_author['user_name']} ({_author['user_id']})", weight=ft.FontWeight.BOLD)
                        ], spacing=8),
                        ft.Text(f"代表作: {_author.get('work_title', '')}", size=13, color=ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.W_500, width=280)
                    ], expand=True),
                    follow_btn,
                    status_text
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER)

                with _ui_update_lock:
                    lv.controls.append(row)

                if _author.get('thumb_url'):
                    containers_to_load.append((_author['thumb_url'], work_thumb_container))
                if _author.get('profile_img_url'):
                    containers_to_load.append((_author['profile_img_url'], icon_container))

                # 5件ごとに画面更新
                if i % 5 == 0:
                    with _ui_update_lock:
                        loading_text.value = f"{i + 1} / {len(authors)} 件を読み込み中..."
                        page.update()

            with _ui_update_lock:
                loading_text.value = f"読み込み完了: {len(authors)} 人"
                page.update()

            # サムネイル高速並行読み込み (8並列)
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import base64 as _b64

            def _fetch_img(url, cont):
                if not session_active[0]:
                    return None, cont
                try:
                    data = client.download_thumbnail_to_memory(url)
                    return data, cont
                except Exception:
                    return None, cont

            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(_fetch_img, url, c) for url, c in containers_to_load]
                updated_count = 0
                for fut in as_completed(futures):
                    if not session_active[0]:
                        break
                    img_data, container = fut.result()
                    if img_data and session_active[0]:
                        b64 = _b64.b64encode(img_data).decode('utf-8')
                        with _ui_update_lock:
                            container.content = ft.Image(
                                src=f"data:image/jpeg;base64,{b64}",
                                fit="cover"
                            )
                            updated_count += 1
                            # 4件ごと、あるいは最後の1件で画面更新してレスポンス向上
                            if updated_count % 4 == 0:
                                page.update()
                with _ui_update_lock:
                    page.update()

        page.run_thread(load_authors)

    check_unfollowed_btn.on_click = lambda _: threading.Thread(target=run_check_unfollowed, daemon=True).start()

    tab_bookmark_content = ft.Column([
        ft.Text("ブックマーク一括ダウンロード", size=20, weight=ft.FontWeight.BOLD),
        ft.Text("あなたのブックマーク作品を一括保存し、保存フォルダに階層化またはフラット配置します。", size=13, color=ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.W_500),
        ft.Divider(),
        ft.Row([bm_target_type_dropdown, bm_rest_type_dropdown, bm_run_btn, check_unfollowed_btn, bm_pause_btn, bm_stop_btn], wrap=True),
        ft.Row([bm_progress_bar, bm_progress_text, bm_remaining_time_text]),
    ], expand=True)

    # --- タブ: 失敗キュー・品質異常リスト ---
    failed_list_view = ft.ListView(expand=True, spacing=5)

    def load_failed_queue_ui():
        jobs = db.get_failed_jobs()
        new_items = []
        if not jobs:
            new_items.append(
                ft.Container(
                    content=ft.Text("現在、失敗キューおよび品質異常の作品はありません ✅", color=ft.Colors.GREEN_300),
                    padding=ft.padding.Padding(0, 5, 0, 5)
                )
            )
        else:
            for job in jobs:
                wid = job["work_id"]
                title = job["title"] or "無題"
                reason = job["error_reason"] or "不明エラー"
                rc = job["retry_count"]
                wtype = job["work_type"] or "illust"

                def make_retry_one(work_id=wid, is_nov=(wtype=="novel")):
                    def _retry(e):
                        append_log(f"作品ID: {work_id} の再試行を開始します...", ft.Colors.BLUE_300)
                        def run_r():
                            try:
                                client = PixivClient(db=db)
                                run_single_work_backup(work_id, is_nov, client, db, log_callback=handle_log)
                                load_failed_queue_ui()
                            except Exception as ex:
                                append_log(f"再試行エラー ({work_id}): {ex}", ft.Colors.RED_400)
                        threading.Thread(target=run_r, daemon=True).start()
                    return _retry

                def make_remove_one(work_id=wid):
                    def _rem(e):
                        db.remove_failed_job(work_id)
                        load_failed_queue_ui()
                    return _rem

                row_card = ft.Container(
                    content=ft.Row([
                        ft.Column([
                            ft.Text(f"{title} (ID: {wid})", weight=ft.FontWeight.BOLD),
                            ft.Text(f"理由: {reason} | リトライ回数: {rc}", size=12, color=ft.Colors.ERROR, weight=ft.FontWeight.W_500),
                        ], expand=True),
                        ft.ElevatedButton("再試行", icon=ft.Icons.REFRESH, on_click=make_retry_one()),
                        ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="削除", on_click=make_remove_one()),
                    ]),
                    bgcolor=ft.Colors.SURFACE_CONTAINER,
                    padding=10,
                    border_radius=6
                )
                new_items.append(row_card)
        with _ui_update_lock:
            failed_list_view.controls.clear()
            failed_list_view.controls.extend(new_items)
            page.update()

    def on_retry_all_failed(e):
        jobs = db.get_failed_jobs()
        if not jobs:
            handle_alert("再試行対象の失敗作品がありません。")
            return
        append_log(f"失敗キュー全件再試行を開始します ({len(jobs)}件)...", ft.Colors.BLUE_300)
        def run_all_r():
            client = PixivClient(db=db)
            for job in jobs:
                wid = job["work_id"]
                is_nov = (job["work_type"] == "novel")
                try:
                    run_single_work_backup(wid, is_nov, client, db, log_callback=handle_log)
                except Exception as ex:
                    append_log(f"再試行エラー ({wid}): {ex}", ft.Colors.RED_400)
            load_failed_queue_ui()
        threading.Thread(target=run_all_r, daemon=True).start()

    def on_clear_all_failed(e):
        db.clear_failed_jobs()
        load_failed_queue_ui()

    def on_export_failed_csv(e):
        import csv
        jobs = db.get_failed_jobs()
        out_path = os.path.join(os.getcwd(), "failed_queue_export.csv")
        try:
            with open(out_path, mode="w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["work_id", "user_id", "title", "work_type", "error_reason", "failed_at", "retry_count"])
                for j in jobs:
                    writer.writerow([j["work_id"], j["user_id"], j["title"], j["work_type"], j["error_reason"], j["failed_at"], j["retry_count"]])
            handle_alert(f"CSV出力完了: {out_path}")
        except Exception as ex:
            handle_alert(f"CSV出力に失敗しました: {ex}")

    tab_failed_content = ft.Column([
        ft.Row([
            ft.Text("ダウンロード失敗キュー・保存品質異常リスト", size=18, weight=ft.FontWeight.BOLD),
            ft.IconButton(ft.Icons.REFRESH, tooltip="リスト更新", on_click=lambda _: load_failed_queue_ui()),
        ]),
        ft.Text("通信エラーや品質異常（0バイト/破損等）が発生した作品一覧です。再試行やCSVエクスポートが行えます。", size=12, color=ft.Colors.ON_SURFACE_VARIANT, weight=ft.FontWeight.W_500),
        ft.Row([
            ft.ElevatedButton("全件再試行", icon=ft.Icons.PLAY_ARROW, color=ft.Colors.WHITE, bgcolor=ft.Colors.BLUE_600, on_click=on_retry_all_failed),
            ft.OutlinedButton("CSV出力", icon=ft.Icons.FILE_DOWNLOAD, on_click=on_export_failed_csv),
            ft.OutlinedButton("全件クリア", icon=ft.Icons.DELETE_OUTLINE, on_click=on_clear_all_failed),
        ]),
        ft.Divider(height=8),
        failed_list_view
    ], expand=True, spacing=8)

    # --- 直近のバックアップ（作者別画像グリッド） ---
    backup_history_grid = ft.GridView(
        expand=True,
        max_extent=160,
        child_aspect_ratio=1.0,
        spacing=10,
        run_spacing=10,
    )

    def load_backup_history_ui():
        # 空にした直後に一度 update() しておくことで、タブが可視化された直後の
        # レイアウトパス（GridView の幅/列数計算）を確定させる。ここを省略すると
        # スレッド側の初回 append がまだ幅0のまま計算された GridView に対して
        # 行われ、以後の更新でも高さ0のまま描画されないことがある(Flet 0.85.3)。
        with _ui_update_lock:
            backup_history_grid.controls.clear()
            backup_history_grid.update()

        def _load_history_thread():
            base_img_dir = db.get_setting("save_path", "Images")
            try:
                cursor = db.conn.execute("""
                    SELECT w.user_id, fu.name as following_name, MAX(w.last_backup) as max_backup
                    FROM works w
                    LEFT JOIN following_users fu ON w.user_id = fu.user_id
                    WHERE w.last_backup IS NOT NULL
                    GROUP BY w.user_id
                    ORDER BY max_backup DESC
                    LIMIT 60
                """)
                rows = cursor.fetchall()
            except Exception as e:
                append_log(f"直近バックアップ履歴取得エラー: {e}", color=ft.Colors.RED_400)
                return

            if not rows:
                append_log("直近バックアップ履歴: 対象データがありません（works.last_backupが未設定）。", color=get_adjusted_color(ft.Colors.GREY_400))
                return

            import glob
            import re
            import zipfile

            # 1リクエストごとに毎回生成すると無駄でエラー源にもなるため、ループの外で1回だけ生成する
            try:
                client_tmp = PixivClient(db=db)
            except Exception as e:
                append_log(f"直近バックアップ履歴: PixivClient初期化エラー: {e}", color=ft.Colors.RED_400)
                return

            def make_click_handler(target_dir):
                return lambda _: open_author_folder(target_dir)

            cards = []
            skipped_no_dir = 0
            skipped_no_image = 0

            for row in rows:
                user_id = row['user_id']
                following_name = row['following_name']

                # 作者フォルダは「ZIPアーカイブ化(auto_archive/zip_all_after_download)」が
                # 有効な場合、ダウンロード直後に append_to_zip() でフォルダごと削除され
                # "作者名(ID).zip" のみが残る運用のため、フォルダとZIPの両方を探索する。
                author_dir = None
                author_dir_name = None
                zip_path = None
                try:
                    if os.path.exists(base_img_dir):
                        dirs = glob.glob(os.path.join(base_img_dir, f"*({user_id})"))
                        if dirs:
                            author_dir = dirs[0]
                            author_dir_name = os.path.basename(author_dir)
                            zip_candidate = author_dir + ".zip"
                            if os.path.exists(zip_candidate):
                                zip_path = zip_candidate
                        else:
                            zips = glob.glob(os.path.join(base_img_dir, f"*({user_id}).zip"))
                            if zips:
                                zip_path = zips[0]
                                author_dir_name = os.path.basename(zip_path)[:-4]
                except Exception as e:
                    append_log(f"直近バックアップ履歴: フォルダ検索エラー(user_id={user_id}): {e}", color=ft.Colors.RED_400)
                    continue

                if not author_dir and not zip_path:
                    skipped_no_dir += 1
                    continue

                display_name = following_name
                if not display_name and author_dir_name:
                    match = re.match(r"^(.*?)\(\d+\)$", author_dir_name)
                    if match:
                        display_name = match.group(1)
                if not display_name:
                    display_name = f"ユーザー({user_id})"

                valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
                found_images = []
                if author_dir:
                    try:
                        for root_dir, _, files in os.walk(author_dir):
                            for file in files:
                                ext = os.path.splitext(file)[1].lower()
                                if ext in valid_exts:
                                    full_path = os.path.join(root_dir, file)
                                    try:
                                        mtime = os.path.getmtime(full_path)
                                        found_images.append((full_path, mtime))
                                    except OSError:
                                        pass
                    except Exception as e:
                        append_log(f"直近バックアップ履歴: 画像探索エラー({author_dir}): {e}", color=ft.Colors.RED_400)
                        continue

                b64_data = None
                if found_images:
                    found_images.sort(key=lambda x: x[1], reverse=True)
                    img_path = found_images[0][0]
                    try:
                        b64_data = client_tmp.get_thumbnail_base64_from_path(img_path, size=(300, 300))
                    except Exception as e:
                        append_log(f"直近バックアップ履歴: サムネイル生成エラー({img_path}): {e}", color=ft.Colors.RED_400)
                elif zip_path:
                    # フォルダに画像が無い(=ZIP化済みで削除された)場合、ZIP内の最新の画像を直接読み込む
                    try:
                        with zipfile.ZipFile(zip_path, 'r') as zf:
                            zip_images = [
                                info for info in zf.infolist()
                                if os.path.splitext(info.filename)[1].lower() in valid_exts
                            ]
                            if zip_images:
                                zip_images.sort(key=lambda i: i.date_time, reverse=True)
                                raw = zf.read(zip_images[0].filename)
                                from PIL import Image
                                import io
                                with Image.open(io.BytesIO(raw)) as img:
                                    img.thumbnail((300, 300))
                                    if img.mode in ("RGBA", "P"):
                                        img = img.convert("RGB")
                                    buf = io.BytesIO()
                                    img.save(buf, format="JPEG", quality=80)
                                    b64_data = base64.b64encode(buf.getvalue()).decode('utf-8')
                    except Exception as e:
                        append_log(f"直近バックアップ履歴: ZIP内画像読み込みエラー({zip_path}): {e}", color=ft.Colors.RED_400)

                if not b64_data:
                    skipped_no_image += 1
                    continue

                open_target = author_dir or zip_path

                # 作者名バー（最初は opacity=0 で非表示。height を明示して
                # opacity=0 でもレイアウト領域が確保された状態を保つ）
                name_container = ft.Container(
                    content=ft.Text(
                        display_name,
                        size=11,
                        weight=ft.FontWeight.BOLD,
                        color=ft.Colors.WHITE,
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        text_align=ft.TextAlign.CENTER
                    ),
                    alignment=ft.alignment.Alignment.CENTER,
                    padding=5,
                    height=34,
                    bgcolor=ft.Colors.with_opacity(0.6, ft.Colors.BLACK),
                    border_radius=ft.BorderRadius.only(bottom_left=10, bottom_right=10),
                    bottom=0,
                    left=0,
                    right=0,
                    opacity=0,
                    animate_opacity=200,
                )

                def make_hover_handler(target_name_container):
                    def _hover(e):
                        with _ui_update_lock:
                            target_name_container.opacity = 1 if e.data == "true" else 0
                            # Stack の奥にある対象コントロール自身を直接 update する
                            # （ジェスチャ元コントロールを update しても子の変更が
                            # 確実に反映されない場合があるため）
                            target_name_container.update()
                    return _hover

                # ヒットテストを確実にするため、Stack 全体を GestureDetector で
                # ラップして on_hover / on_tap を検出する（Container.on_hover は
                # Stack 内に Positioned な子要素が重なると判定が不安定になることがある）
                card = ft.Container(
                    width=140,
                    height=140,
                    border_radius=10,
                    bgcolor=ft.Colors.SURFACE_CONTAINER,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    tooltip=f"{display_name} ({user_id})\nクリックして開く",
                    content=ft.GestureDetector(
                        mouse_cursor=ft.MouseCursor.CLICK,
                        on_tap=make_click_handler(open_target),
                        on_hover=make_hover_handler(name_container),
                        content=ft.Stack(
                            [
                                ft.Image(
                                    src=f"data:image/jpeg;base64,{b64_data}",
                                    width=140,
                                    height=140,
                                    fit=ft.BoxFit.COVER,
                                ),
                                name_container,
                            ],
                            width=140,
                            height=140,
                        ),
                    ),
                )
                cards.append(card)

                # 進捗表示のため10件ごとに反映（page.update()ではなくコントロール単位のupdate()を使い、
                # 別スレッドからのUI更新をこのGridViewに限定してレースを避ける）
                if len(cards) % 10 == 0:
                    with _ui_update_lock:
                        backup_history_grid.controls = list(cards)
                        backup_history_grid.update()

            with _ui_update_lock:
                backup_history_grid.controls = cards
                backup_history_grid.update()

            if cards:
                append_log(f"直近のバックアップ: {len(cards)}件のタイルを表示しました。", color=ft.Colors.GREEN_400)
            else:
                append_log(
                    f"直近のバックアップ: 表示できるタイルがありませんでした "
                    f"(フォルダ/ZIP未検出:{skipped_no_dir}件 / 画像なし:{skipped_no_image}件)。",
                    color=ft.Colors.ERROR
                )

        page.run_thread(_load_history_thread)

    tab_backup_history_content = ft.Column([
        ft.Row([
            ft.Text("直近のバックアップ", size=20, weight=ft.FontWeight.BOLD),
            ft.IconButton(ft.Icons.REFRESH, tooltip="更新", on_click=lambda _: load_backup_history_ui())
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Divider(),
        backup_history_grid
    ], expand=True)

    # --- タブ切り替え ---

    tab1_container = ft.Container(content=tab1_content, padding=10, visible=True, expand=True)
    tab2_container = ft.Container(content=tab2_content, padding=10, visible=False, expand=True)
    tab_bookmark_container = ft.Container(content=tab_bookmark_content, padding=10, visible=False, expand=True)
    tab_queue_container = ft.Container(content=tab_queue_content, padding=10, visible=False, expand=True)
    tab_failed_container = ft.Container(content=tab_failed_content, padding=10, visible=False, expand=True)
    tab_extension_container = ft.Container(content=tab_extension_content, padding=10, visible=False)
    tab_backup_history_container = ft.Container(content=tab_backup_history_content, padding=10, visible=False, expand=True)

    tab_containers = [tab1_container, tab2_container, tab_bookmark_container, tab_queue_container, tab_failed_container, tab_extension_container, tab_backup_history_container]
    tab_buttons = []
    current_tab_idx = [0]

    def on_tab_change(idx):
        if idx == 99:
            open_save_folder()
            return

        current_tab_idx[0] = idx
        with _ui_update_lock:
            for i, container in enumerate(tab_containers):
                container.visible = (i == idx)

            for btn in tab_buttons:
                if btn.data != 99:
                    is_selected = (btn.data == idx)
                    btn.style = ft.ButtonStyle(
                        color=ft.Colors.PRIMARY if is_selected else ft.Colors.ON_SURFACE,
                        bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.PRIMARY) if is_selected else ft.Colors.TRANSPARENT,
                    )

            history_btn.icon_color = ft.Colors.PRIMARY if idx == 6 else None
            # 拡張機能タブ(idx=5)と直近バックアップタブ(idx=6)ではログを非表示
            log_container.visible = (idx not in [5, 6])

            # 可視化の更新を先に確定させてから、直近バックアップグリッドへの
            # データ投入を開始する。同時に行うと GridView がまだ幅0のまま
            # レイアウトされ、以後のスレッド側 update() でも高さ0のまま
            # 描画されないことがあるため(Flet 0.85.3)、順序を分離する。
            page.update()

        if idx == 6:
            load_backup_history_ui()

    def create_tab_btn(idx, label, icon):
        btn = ft.TextButton(
            label, icon=icon,
            data=idx,
            on_click=lambda e, i=idx: on_tab_change(i),
            style=ft.ButtonStyle(
                color=ft.Colors.PRIMARY if idx == 0 else ft.Colors.ON_SURFACE,
                bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.PRIMARY) if idx == 0 else ft.Colors.TRANSPARENT,
            )
        )
        tab_buttons.append(btn)
        return btn

    btn_follow = create_tab_btn(1, "フォロー中一括", ft.Icons.GROUP)
    follow_tab = ft.Row([btn_follow, follow_count_badge], spacing=0)

    custom_tab_bar = ft.Row([
        create_tab_btn(0, "個別DL", ft.Icons.PERSON),
        follow_tab,
        create_tab_btn(2, "ブックマーク", ft.Icons.BOOKMARK),
        create_tab_btn(3, "キュー", ft.Icons.QUEUE),
        create_tab_btn(4, "失敗リスト", ft.Icons.REPORT_PROBLEM),
        ft.Container(expand=True),
        create_tab_btn(5, "拡張機能", ft.Icons.EXTENSION),
    ], alignment=ft.MainAxisAlignment.START, scroll=ft.ScrollMode.AUTO)

    page.add(
        ft.Row([
            ft.Column([
                ft.Row([
                    ft.Text("PixivVault", size=32, weight=ft.FontWeight.BOLD),
                ]),
                login_status_text,
                ft.Row(
                    [app_connection_icon, app_connection_text, web_bridge_toggle,
                     ft.Text("iOS連携", size=11, color=ft.Colors.ON_SURFACE_VARIANT)],
                    spacing=6
                ),
            ]),
            ft.Row([history_btn, open_folder_btn, theme_toggle_btn, cookie_picker_btn, settings_btn], spacing=5)
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        cookie_banner,
        custom_tab_bar,
        tab1_container,
        tab2_container,
        tab_bookmark_container,
        tab_queue_container,
        tab_failed_container,
        tab_extension_container,
        tab_backup_history_container,
        log_container
    )


    # --- 起動時バックグラウンド処理 ---
    def run_auto_sync_thread(my_id):
        try:
            client = PixivClient(db=db)
            users  = client.get_following_users(my_user_id=my_id, rest_type="show", log_callback=None)
            users.extend(client.get_following_users(my_user_id=my_id, rest_type="hide", log_callback=None))
            db.save_following_users(users)
            db.set_setting("last_sync_date", datetime.now().isoformat())
            append_log(f"起動時自動同期完了 ({len(users)}人の作者)", color=ft.Colors.GREEN_400)
            load_follow_list_ui()
        except Exception as e:
            append_log(f"自動同期失敗: {e}", color=ft.Colors.RED_400)

    def trigger_auto_sync(my_id):
        last_sync = db.get_setting("last_sync_date")
        should_sync = True
        if last_sync:
            try:
                delta = (datetime.now() - datetime.fromisoformat(last_sync)).total_seconds()
                should_sync = delta > 24 * 3600
            except Exception:
                pass
        if should_sync:
            append_log("起動時フォローリスト自動同期を開始します...", color=ft.Colors.BLUE_300)
            threading.Thread(target=run_auto_sync_thread, args=(my_id,), daemon=True).start()

    def check_login_status(on_cookie_imported=False, auto_retried=False):
        old_user_id = db.get_setting("my_user_id", "")
        status_info = PixivClient.check_cookie_status("cookies.txt")
        st = status_info.get("status", "expired")
        msg = status_info.get("message", "")
        uid = status_info.get("user_id")
        days_left = status_info.get("days_left", 0)

        # 起動時など、期限切れや1週間以下(黄色・赤色)でまだ自動更新試行していなければ、裏で全自動抽出にトライ！
        if (st == "expired" or st in ["warning_yellow", "warning_red", "warning"]) and not on_cookie_imported and not auto_retried:
            append_log("Cookie有効期限チェック: ブラウザ (Chrome/Edge/Firefox) から最新Cookieの全自動抽出を試行します...", color=ft.Colors.BLUE_300)
            try:
                auto_info = PixivClient.auto_extract_browser_cookies("cookies.txt")
                auto_st = auto_info.get("status", "expired")
                if auto_st in ["valid", "warning_yellow", "warning_red"]:
                    status_info = auto_info
                    st = auto_st
                    msg = auto_info.get("message", "")
                    uid = auto_info.get("user_id")
                    days_left = auto_info.get("days_left", 0)
                    append_log(f"ブラウザ ({auto_info.get('browser')}) からの Cookie 自動抽出・更新に成功しました！", color=ft.Colors.GREEN_400)
                    on_cookie_imported = True
                else:
                    append_log("ブラウザからの自動抽出を試みましたが、有効なセッションが見つかりませんでした。ブラウザでPixivへログインし、ページを開くと自動で連携されます。", color=ft.Colors.ON_SURFACE_VARIANT)
            except Exception as ex:
                logger.debug(f"自動抽出エラー: {ex}")

        with _ui_update_lock:
            if st == "valid":
                login_status_text.value = f"● [有効] {msg}"
                login_status_text.color = ft.Colors.GREEN_400
                cookie_banner.visible = False
            elif st == "warning_yellow":
                login_status_text.value = f"▲ [注意/残り1週間以内] {msg}"
                login_status_text.color = ft.Colors.YELLOW_400
                cookie_banner.visible = False
            elif st in ["warning", "warning_red"]:
                login_status_text.value = f"▲ [警告/残り3日以内] {msg}"
                login_status_text.color = ft.Colors.RED_400
                cookie_banner.visible = False
            else:
                login_status_text.value = f"× [未認証/期限切れ] {msg}"
                login_status_text.color = ft.Colors.RED_400
                cookie_banner.visible = True
            page.update()

        if uid and st in ["valid", "warning_yellow", "warning_red", "warning"]:
            if str(uid) != str(old_user_id) and str(old_user_id) != "":
                # 別アカウントのCookieに切り替わった場合のみ全削除・再同期
                append_log(f"別アカウント(ID: {uid})のCookieを読み込みました。旧フォロー中データを削除・再取得します。", color=ft.Colors.ORANGE_400)
                db.clear_following_users()
                db.set_setting("my_user_id", str(uid))
                threading.Thread(target=sync_follow_list, daemon=True).start()
            elif on_cookie_imported:
                # 同アカウントのCookie更新・確認時は全削除せずに差分同期のみ行う
                append_log("Cookie を確認・更新しました。", color=ft.Colors.GREEN_400)
                db.set_setting("my_user_id", str(uid))
                trigger_auto_sync(uid)
            else:
                db.set_setting("my_user_id", str(uid))
                trigger_auto_sync(uid)

    # 初期化
    gui_trigger_cookie_check[0] = lambda: threading.Thread(target=check_login_status, daemon=True).start()
    load_follow_list_ui()
    load_failed_queue_ui()
    threading.Thread(target=check_login_status, daemon=True).start()

