import os
import sys
import shutil
import tkinter as tk
from tkinter import filedialog
import flet as ft
import threading
import logging
from datetime import datetime
from pixiv_client import PixivClient
from database import Database
from core import run_backup, export_data, run_batch_backup, download_bookmarks, get_unfollowed_bookmark_authors, run_single_work_backup
import registry_helper
import base64

logger = logging.getLogger(__name__)

gui_log_callback = [None]
gui_queue_log_callback = [None]
gui_trigger_cookie_check = [None]
is_downloading_active = [False]
request_stop_all = [None]
# server.py（拡張機能連携）からもダウンロード中フラグを正しく管理できるようにするためのフック。
# gui.py 内の GUI 発のフローと合算して is_downloading_active を管理する。
gui_set_flow_active = [None]


def main_window(page: ft.Page):
    page.title = "PixivVault"
    page.theme_mode = ft.ThemeMode.DARK
    page.window.width = 800
    page.window.height = 600

    db = Database()

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
        border_radius=5, padding=10, expand=True, bgcolor="#1A1C1E"
    )

    list_expanded = [False]
    def toggle_list_expansion(e=None):
        list_expanded[0] = not list_expanded[0]
        log_container.visible = not list_expanded[0]
        page.update()

    def append_log(msg: str, color: str = ft.Colors.WHITE):
        time_str = datetime.now().strftime("%y%m%d %H:%M:%S")
        log_area.controls.append(ft.Text(f"[{time_str}] {msg}", color=color, selectable=True, size=13))
        page.update()
    def handle_log(msg: str):
        append_log(msg)
    def handle_alert(msg: str):
        append_log(f"[!] {msg}", color=ft.Colors.RED_400)
    
    gui_log_callback[0] = handle_log

    login_status_text = ft.Text("ログインチェック中...", color=ft.Colors.BLUE_200, size=12)

    def clear_user_id_field(e):
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
    remaining_time_text = ft.Text("", size=12, color=ft.Colors.BLUE_200)

    progress_history = []

    def handle_progress(current: int, total: int, elapsed_sec: float = 0, p_bar=progress_bar, p_text=progress_text, r_text=remaining_time_text, history=None):
        # history未指定時は個別DL(タブ1)用のリストを使う。ブックマークDLなど別フローは
        # 専用のリストを渡すことで、同時実行時にETA計算が混線しないようにする。
        if history is None:
            history = progress_history
        p_bar.value = current / total if total > 0 else 0
        p_text.value = f"{current} / {total}"

        if elapsed_sec > 0 and current < total:
            history.append((current, elapsed_sec))
            if len(history) > 10:
                history.pop(0)

            if len(history) >= 2:
                items_done = history[-1][0] - history[0][0]
                time_taken = history[-1][1] - history[0][1]
                if time_taken > 0 and items_done > 0:
                    speed = items_done / time_taken
                    remaining_items = total - current
                    eta_sec = remaining_items / speed
                    mins, secs = divmod(int(eta_sec), 60)
                    if mins > 0:
                        r_text.value = f"残り約{mins}分{secs}秒"
                    else:
                        r_text.value = f"残り約{secs}秒"
                else:
                    r_text.value = "残り時間計算中..."
            else:
                r_text.value = "残り時間計算中..."
        elif current == total:
            r_text.value = ""
            history.clear()

        page.update()

    def set_ui_disabled_single(disabled: bool, is_running: bool = False):
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
        user_id = user_id_field.value.strip()
        if not user_id:
            append_log("ユーザーIDを入力してください。", color=ft.Colors.ORANGE_300)
            return
        if not os.path.exists("cookies.txt"):
            handle_alert("cookies.txt が見つかりません。設定ボタンからインポートしてください。")
            return

        is_full = (mode_dropdown.value == "full")
        target_type = target_type_dropdown.value
        append_log("--- 個別ダウンロードを開始します ---", color=ft.Colors.BLUE_300)
        progress_bar.value = 0
        progress_bar.visible = True
        progress_text.visible = True
        single_stop_event.clear()
        single_pause_event.clear()
        set_ui_disabled_single(True, is_running=True)
        _set_flow_active("single", True)

        try:
            client = PixivClient()
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
            progress_bar.visible = False
            progress_text.visible = False
            remaining_time_text.value = ""
            progress_history.clear()
            _set_flow_active("single", False)
            set_ui_disabled_single(False, is_running=False)

    run_btn.on_click = lambda _: threading.Thread(target=run_backup_thread, daemon=True).start()

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

    batch_target_type_dropdown = ft.Dropdown(
        label="対象", width=160,
        options=[
            ft.DropdownOption(key="illust", text="イラスト・漫画"),
            ft.DropdownOption(key="novel", text="小説"),
            ft.DropdownOption(key="both", text="両方"),
        ],
        value="both"
    )
    

    def clear_search_field(e):
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
    sort_by_dropdown.on_select    = lambda e: load_follow_list_ui()
    sort_order_dropdown.on_select = lambda e: load_follow_list_ui()

    batch_run_btn    = ft.ElevatedButton("一括ダウンロード実行", icon=ft.Icons.PLAY_ARROW)
    batch_pause_btn  = ft.ElevatedButton("一時停止", icon=ft.Icons.PAUSE, disabled=True)
    batch_stop_btn   = ft.ElevatedButton("停止", icon=ft.Icons.STOP, disabled=True)
    select_all_btn   = ft.TextButton("すべて選択")
    deselect_all_btn = ft.TextButton("選択解除")
    select_favorite_btn = ft.TextButton("お気に入りのみ選択")

    batch_progress_bar  = ft.ProgressBar(width=400, value=0, visible=False)
    batch_progress_text = ft.Text("0 / 0", visible=False)
    batch_remaining_time_text = ft.Text("", size=12, color=ft.Colors.BLUE_200)

    def load_follow_list_ui(search_val_override=None):
        # 現在のチェックボックスの選択状態を退避
        saved_states = {uid: cb.value for uid, cb in follow_checkboxes.items()}
        
        follow_list_view.controls.clear()
        follow_checkboxes.clear()
        users = db.get_following_users(sort_by=sort_by_dropdown.value, sort_order=sort_order_dropdown.value)
        
        # 検索値: 引数優先、なければフィールドから取得
        search_q = search_val_override if search_val_override is not None else (search_field.value or "")
        search_q = search_q.strip().lower()
        if search_q:
            users = [u for u in users if search_q in (u.get('name') or '').lower() or search_q in str(u.get('user_id', '')).lower()]
        
        follow_count_text.value = str(len(users))
        
        append_log(f"[システム] リスト更新: 検索='{search_q}', 件数={len(users)}")

        def toggle_zip(e, uid):
            btn = e.control
            is_zipped = btn.icon == ft.Icons.ARCHIVE
            new_val = not is_zipped
            db.set_zipped(uid, new_val)
            btn.icon = ft.Icons.ARCHIVE if new_val else ft.Icons.ARCHIVE_OUTLINED
            btn.icon_color = ft.Colors.BLUE_400 if new_val else ft.Colors.GREY_500
            page.update()

        def toggle_favorite(e, uid):
            btn = e.control
            is_fav = btn.icon == ft.Icons.STAR
            new_val = not is_fav
            db.set_favorite(uid, new_val)
            btn.icon = ft.Icons.STAR if new_val else ft.Icons.STAR_BORDER
            btn.icon_color = ft.Colors.YELLOW_600 if new_val else ft.Colors.GREY_500
            page.update()

        for u in users:
            label = f"{u['name']} (ID:{u['user_id']})"
            if u.get('last_downloaded'):
                label += f" [最終: {u['last_downloaded'][:10]}]"
                
            # 退避しておいた選択状態を復元（デフォルトは False）
            cb = ft.Checkbox(value=saved_states.get(u['user_id'], False))
            follow_checkboxes[u['user_id']] = cb
            
            def on_label_tap(e, cb_ref=cb):
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
                        ft.Text("この作者のみ適用するオーバーライド設定です。", size=12, color=ft.Colors.GREY_400),
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
            follow_list_view.controls.append(row)

        # 明示的にListViewとページを更新
        follow_list_view.update()
        page.update()
        
    search_field.on_change  = lambda e: load_follow_list_ui(search_val_override=e.control.value)

    def set_ui_disabled_batch(disabled: bool, is_running: bool = False):
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

    def handle_batch_progress(idx: int, total: int, user_id: str, elapsed_sec: float = 0):
        batch_progress_bar.value  = idx / total if total > 0 else 0
        batch_progress_text.value = f"作者 {idx} / {total}"
        
        if elapsed_sec > 0 and idx < total:
            batch_progress_history.append((idx, elapsed_sec))
            if len(batch_progress_history) > 5:
                batch_progress_history.pop(0)
                
            if len(batch_progress_history) >= 2:
                items_done = batch_progress_history[-1][0] - batch_progress_history[0][0]
                time_taken = batch_progress_history[-1][1] - batch_progress_history[0][1]
                if time_taken > 0 and items_done > 0:
                    speed = items_done / time_taken
                    remaining_items = total - idx
                    eta_sec = remaining_items / speed
                    mins, secs = divmod(int(eta_sec), 60)
                    if mins > 0:
                        batch_remaining_time_text.value = f"残り約{mins}分{secs}秒"
                    else:
                        batch_remaining_time_text.value = f"残り約{secs}秒"
                else:
                    batch_remaining_time_text.value = "残り時間計算中..."
            else:
                batch_remaining_time_text.value = "残り時間計算中..."
        elif idx == total:
            batch_remaining_time_text.value = ""
            batch_progress_history.clear()

        page.update()

    batch_summary_text = ft.Row(spacing=10, wrap=True)
    batch_summary_card = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.ANALYTICS, color=ft.Colors.BLUE_400),
            ft.Text("実行結果サマリー: ", weight=ft.FontWeight.BOLD),
            batch_summary_text
        ], wrap=True),
        bgcolor="#252A30",
        padding=10,
        border_radius=8,
        border=ft.Border(
            top=ft.BorderSide(1, ft.Colors.BLUE_400), bottom=ft.BorderSide(1, ft.Colors.BLUE_400),
            left=ft.BorderSide(1, ft.Colors.BLUE_400), right=ft.BorderSide(1, ft.Colors.BLUE_400)
        ),
        visible=False
    )

    def run_batch_thread():
        selected_ids = [uid for uid, cb in follow_checkboxes.items() if cb.value]
        if not selected_ids:
            append_log("ダウンロードする作者を選択してください。", color=ft.Colors.ORANGE_300)
            return
        if not os.path.exists("cookies.txt"):
            handle_alert("cookies.txt が見つかりません。")
            return

        append_log(f"--- {len(selected_ids)}人の一括ダウンロードを開始します ---", color=ft.Colors.BLUE_300)
        batch_progress_bar.value   = 0
        batch_progress_bar.visible = True
        batch_progress_text.visible = True
        batch_stop_event.clear()
        batch_pause_event.clear()
        set_ui_disabled_batch(True, is_running=True)
        _set_flow_active("batch", True)

        try:
            client = PixivClient()
            target_type = batch_target_type_dropdown.value
            batch_summary_card.visible = False
            stats = run_batch_backup(
                user_ids=selected_ids, client=client, db=db, is_full=False, target_type=target_type,
                log_callback=handle_log, progress_callback=None, alert_callback=handle_alert,
                stop_event=batch_stop_event, pause_event=batch_pause_event, batch_progress_callback=handle_batch_progress
            )
            if stats and isinstance(stats, dict):
                batch_summary_text.controls = [
                    ft.Container(content=ft.Text(f"〇 新規: {stats.get('new_count', 0)}件", color=ft.Colors.GREEN_300, weight=ft.FontWeight.BOLD), bgcolor="#1F3A2A", padding=4, border_radius=4),
                    ft.Container(content=ft.Text(f"△ 更新: {stats.get('updated_count', 0)}件", color=ft.Colors.BLUE_300, weight=ft.FontWeight.BOLD), bgcolor="#1F2A3A", padding=4, border_radius=4),
                    ft.Container(content=ft.Text(f"× 削除検知: {stats.get('deleted_count', 0)}件", color=ft.Colors.RED_300 if stats.get('deleted_count', 0) > 0 else ft.Colors.GREY_400, weight=ft.FontWeight.BOLD), bgcolor="#3A1F1F" if stats.get('deleted_count', 0) > 0 else "#2A2A2A", padding=4, border_radius=4),
                    ft.Container(content=ft.Text(f"↺ 復帰: {stats.get('restored_count', 0)}件", color=ft.Colors.TEAL_300, weight=ft.FontWeight.BOLD), bgcolor="#1F3A3A", padding=4, border_radius=4),
                    ft.Text(f"スキップ: {stats.get('skipped_count', 0)}件 | エラー: {stats.get('failed_count', 0)}件", size=12, color=ft.Colors.GREY_400)
                ]
                batch_summary_card.visible = True
            append_log("--- 一括ダウンロードが完了しました ---", color=ft.Colors.GREEN_400)
        except Exception as e:
            if batch_stop_event.is_set():
                handle_alert("一括処理が中止されました。")
            else:
                handle_alert(f"エラー: {e}")
        finally:
            batch_progress_bar.visible  = False
            batch_progress_text.visible = False
            batch_remaining_time_text.value = ""
            batch_progress_history.clear()
            _set_flow_active("batch", False)
            set_ui_disabled_batch(False, is_running=False)
            load_follow_list_ui()

    batch_run_btn.on_click = lambda _: threading.Thread(target=run_batch_thread, daemon=True).start()

    def on_batch_pause(e):
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
    batch_stop_btn.on_click  = lambda _: batch_stop_event.set()
    select_all_btn.on_click   = lambda _: [setattr(cb, 'value', True) for cb in follow_checkboxes.values()] or page.update()
    deselect_all_btn.on_click = lambda _: [setattr(cb, 'value', False) for cb in follow_checkboxes.values()] or page.update()
    
    def on_select_favorite(e):
        favs = [str(u['user_id']) for u in db.get_favorite_users()]
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
    stop_btn.on_click  = lambda _: single_stop_event.set()

    def run_export_thread():
        append_log("--- エクスポートを開始します ---", color=ft.Colors.BLUE_300)
        try:
            export_data(db=db, log_callback=handle_log)
            append_log("--- エクスポートが完了しました ---", color=ft.Colors.GREEN_400)
        except Exception as e:
            handle_alert(f"エラー: {e}")
    export_btn.on_click = lambda _: threading.Thread(target=run_export_thread, daemon=True).start()

    # --- 設定ダイアログ ---
    cookie_status_text = ft.Text("", selectable=True, visible=False, size=12)
    sync_status_text   = ft.Text("", selectable=True, visible=False, size=12)
    save_path_text     = ft.Text(
        f"現在の保存先: {db.get_setting('save_path', 'Images')}",
        selectable=True, color=ft.Colors.BLUE_200, size=12
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
                cookie_status_text.value = "[完了] cookies.txt をインポートしました。"
                cookie_status_text.color = ft.Colors.GREEN_400
                cookie_status_text.visible = True
                threading.Thread(target=check_login_status, daemon=True).start()
            else:
                cookie_status_text.value = "キャンセルされました。"
                cookie_status_text.color = ft.Colors.GREY_400
                cookie_status_text.visible = True
        except Exception as ex:
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
                save_path_text.value = f"現在の保存先: {folder_path}"
                append_log(f"保存先フォルダーを {folder_path} に変更しました。")
            else:
                append_log("フォルダー選択がキャンセルされました。")
        except Exception as ex:
            append_log(f"フォルダー選択エラー: {ex}", color=ft.Colors.RED_400)
        page.update()

    def sync_follow_list():
        sync_status_text.value = "Pixivから同期中..."
        sync_status_text.color = ft.Colors.BLUE_400
        sync_status_text.visible = True
        page.update()
        try:
            client = PixivClient()
            my_id  = client.get_my_user_id()
            users  = client.get_following_users(my_user_id=my_id, rest_type="show", log_callback=handle_log)
            users.extend(client.get_following_users(my_user_id=my_id, rest_type="hide", log_callback=handle_log))
            db.save_following_users(users)
            db.set_setting("last_sync_date", datetime.now().isoformat())
            sync_status_text.value = f"[完了] 同期完了 ({len(users)}人の作者)"
            sync_status_text.color = ft.Colors.GREEN_400
            sync_status_text.visible = True
            load_follow_list_ui()
        except Exception as e:
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
        width=300
    )
    novel_format_dropdown.on_change = on_novel_format_change

    def on_auto_check_interval_change(e):
        db.set_setting("auto_check_interval_hours", e.control.value)

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
        width=300
    )
    auto_check_dropdown.on_change = on_auto_check_interval_change

    def on_enable_notifications_change(e):
        db.set_setting("enable_notifications", "1" if e.control.value == "1" else "0")

    notifications_dropdown = ft.Dropdown(
        label="新着通知のON/OFF",
        options=[
            ft.DropdownOption("1", "ON (通知する)"),
            ft.DropdownOption("0", "OFF (通知しない)"),
        ],
        value=db.get_setting("enable_notifications", "1"),
        width=300
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
    
    cache_status_text = ft.Text("", size=12)

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
        if deleted_count > 0:
            cache_status_text.value = f"キャッシュを削除しました ({deleted_count}件)"
            cache_status_text.color = ft.Colors.GREEN_400
        else:
            cache_status_text.value = "キャッシュはありませんでした。"
            cache_status_text.color = ft.Colors.GREY_400
        append_log(f"画像のキャッシュを削除しました ({deleted_count}件)")
        page.update()

    clear_cache_btn = ft.ElevatedButton(
        "キャッシュを削除する",
        icon=ft.Icons.DELETE_OUTLINE,
        on_click=on_clear_cache_click
    )

    clear_cache_container = ft.Container(
        content=ft.Column([
            ft.Text("未フォロー作者確認時のサムネイル画像キャッシュを削除します。", size=12, color=ft.Colors.GREY_400),
            ft.Row([clear_cache_btn, cache_status_text], vertical_alignment=ft.CrossAxisAlignment.CENTER)
        ], spacing=4),
        padding=ft.padding.Padding(10, 6, 0, 6)
    )

    advanced_settings = ft.ExpansionTile(
        title=ft.Text("Advanced / 高度な設定", weight=ft.FontWeight.BOLD),
        controls=[zip_all_checkbox, minimize_to_tray_checkbox, clear_cache_container]
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

    settings_dialog = ft.AlertDialog(
        title=ft.Text("設定"),
        content=ft.Column([
            ft.Text("1. Cookie のインポート", weight=ft.FontWeight.BOLD),
            ft.Text("Pixivのログイン状態を引き継ぐための cookies.txt を選択してください。", size=12, color=ft.Colors.GREY_400),
            ft.ElevatedButton("ファイルを選択", icon=ft.Icons.UPLOAD_FILE,
                              on_click=lambda _: threading.Thread(target=_run_cookie_file_picker, daemon=True).start()),
            cookie_status_text,
            ft.Divider(),
            ft.Text("2. フォロー中リストの同期", weight=ft.FontWeight.BOLD),
            ft.Text("Pixivからフォロー中の作者情報を取得し、DBに保存します。", size=12, color=ft.Colors.GREY_400),
            ft.ElevatedButton("リストを同期", icon=ft.Icons.SYNC,
                              on_click=lambda _: threading.Thread(target=sync_follow_list, daemon=True).start()),
            sync_status_text,
            ft.Divider(),
            ft.Text("3. 保存フォルダーの設定", weight=ft.FontWeight.BOLD),
            ft.Text("画像のダウンロード先フォルダーを指定します（再起動後も保持）。", size=12, color=ft.Colors.GREY_400),
            ft.ElevatedButton("フォルダーを選択", icon=ft.Icons.FOLDER_OPEN,
                              on_click=lambda _: threading.Thread(target=_run_folder_picker, daemon=True).start()),
            save_path_text,
            ft.Divider(),
            ft.Text("4. その他の設定", weight=ft.FontWeight.BOLD),
            novel_format_dropdown,
            auto_check_dropdown,
            notifications_dropdown,
            bookmark_download_mode_dropdown,
            ft.Divider(),
            ft.Text("5. 通信・429レート制限制御", weight=ft.FontWeight.BOLD),
            download_interval_dropdown,
            api_retry_count_dropdown,
            api_retry_wait_dropdown,
            advanced_settings,
            ft.Row([
                ft.Text("v3.0 build260715", size=11, color=ft.Colors.GREY_600)
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Row([
                ft.TextButton("閉じる", on_click=lambda _: page.pop_dialog())
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

    open_folder_btn = ft.IconButton(
        icon=ft.Icons.FOLDER_OPEN, tooltip="保存フォルダを開く",
        on_click=open_save_folder
    )

    settings_btn = ft.IconButton(
        icon=ft.Icons.SETTINGS, tooltip="設定",
        on_click=lambda _: page.show_dialog(settings_dialog)
    )

    # --- 拡張機能タブ ---
    ext_status_text = ft.Text("未登録", color=ft.Colors.GREY_400, weight=ft.FontWeight.BOLD)
    
    def update_ext_status():
        import sys
        if sys.platform != "win32":
            ext_status_text.value = "有効（macOSのURLスキームで自動解決されます）"
            ext_status_text.color = ft.Colors.GREEN_400
        else:
            is_registered = registry_helper.check_protocol_registered()
            if is_registered:
                ext_status_text.value = "有効（レジストリ登録済み）"
                ext_status_text.color = ft.Colors.GREEN_400
            else:
                ext_status_text.value = "無効（レジストリ未登録）"
                ext_status_text.color = ft.Colors.GREY_400
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

    tab_extension_content = ft.Column([
        ft.Text("ブラウザ拡張機能 連携設定", size=20, weight=ft.FontWeight.BOLD),
        ft.Divider(),
        ft.Text("PixivVaultのブラウザ拡張機能を使うと、Pixivのページから直接ダウンロード指示を送ることができます。", size=14),
        ft.Container(height=10),
        ft.Text("現在の状態:", weight=ft.FontWeight.BOLD),
        ext_status_text,
        ft.Container(height=10),
        ft.Container(
            content=ft.Column([
                ft.Text("⚠️ 自動起動について", weight=ft.FontWeight.BOLD, color=ft.Colors.ORANGE_400),
                ft.Text(
                    "アプリが起動していない時にボタンを押した場合、ブラウザから自動でこのアプリを起動することができます。\n"
                    "この機能を有効にするには、Windowsのレジストリ（HKEY_CURRENT_USER）にカスタムURLスキームを書き込みます。",
                    size=12, color=ft.Colors.GREY_300
                ),
            ]),
            bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.ORANGE),
            padding=10,
            border_radius=8
        ),
        ft.Row([
            ft.ElevatedButton("自動起動を有効化", icon=ft.Icons.CHECK, color=ft.Colors.WHITE, bgcolor=ft.Colors.BLUE_600, on_click=on_register_ext, disabled=(sys.platform != "win32")),
            ft.OutlinedButton("自動起動を無効化", icon=ft.Icons.CLOSE, on_click=on_unregister_ext, disabled=(sys.platform != "win32")),
        ]),
    ], spacing=10)
    
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
        bgcolor="#1A1C1E",
        border_radius=5,
        padding=10,
        expand=True
    )

    def append_queue_log(msg: str, color: str = ft.Colors.WHITE):
        time_str = datetime.now().strftime("%y%m%d %H:%M:%S")
        queue_log_area.controls.append(
            ft.Text(f"[{time_str}] {msg}", color=color, selectable=True, size=13)
        )
        page.update()

    gui_queue_log_callback[0] = append_queue_log

    tab_queue_content = ft.Column([
        ft.Row([
            ft.Icon(ft.Icons.LIST_ALT, color=ft.Colors.BLUE_400),
            ft.Text("拡張機能からのダウンロードキューログ", size=16, weight=ft.FontWeight.BOLD),
        ]),
        ft.Text("ブラウザ拡張機能から追加されたダウンロードリクエストの進捗をリアルタイム表示します。", size=13, color=ft.Colors.WHITE70),
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

    def run_bookmark_download_thread():
        my_user_id = db.get_setting("my_user_id", "")
        if not my_user_id:
            handle_alert("ログインチェックを完了してください。")
            return
            
        bm_run_btn.disabled = True
        bm_pause_btn.disabled = False
        bm_stop_btn.disabled = False
        bm_progress_bar.value = 0
        bm_progress_bar.visible = True
        bm_progress_text.visible = True
        _set_flow_active("bookmark", True)
        bm_stop_event.clear()
        bm_pause_event.clear()
        page.update()

        try:
            client = PixivClient()
            download_bookmarks(
                db=db, client=client, my_user_id=my_user_id,
                target_type=bm_target_type_dropdown.value,
                rest_type=bm_rest_type_dropdown.value,
                log_callback=append_log, progress_callback=lambda c, t, e: handle_progress(c, t, e, bm_progress_bar, bm_progress_text, bm_remaining_time_text, bm_progress_history),
                alert_callback=handle_alert, stop_event=bm_stop_event, pause_event=bm_pause_event
            )
            append_log("--- ブックマーク一括DL完了 ---", color=ft.Colors.GREEN_400)
        except Exception as e:
            if bm_stop_event.is_set():
                handle_alert("処理が中止されました。")
            else:
                handle_alert(f"エラー: {e}")
        finally:
            bm_run_btn.disabled = False
            bm_pause_btn.disabled = True
            bm_stop_btn.disabled = True
            bm_progress_bar.visible = False
            bm_progress_text.visible = False
            bm_remaining_time_text.value = ""
            bm_progress_history.clear()
            _set_flow_active("bookmark", False)
            page.update()

    bm_run_btn.on_click = lambda _: threading.Thread(target=run_bookmark_download_thread, daemon=True).start()
    bm_stop_btn.on_click = lambda _: bm_stop_event.set()

    def on_bm_pause(e):
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

        check_unfollowed_btn.disabled = True
        page.update()

        try:
            client = PixivClient()
            authors = get_unfollowed_bookmark_authors(
                db, client, my_user_id,
                target_type=bm_target_type_dropdown.value,
                rest_type=bm_rest_type_dropdown.value,
                log_callback=append_log
            )
        except Exception as e:
            logger.exception(f"未フォロー作者の抽出に失敗しました: {e}")
            handle_alert(f"抽出エラー: {e}")
            check_unfollowed_btn.disabled = False
            page.update()
            return

        if not authors:
            handle_alert("未フォローの作者は見つかりませんでした。")
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
                    bgcolor=ft.Colors.GREY_800,
                    border_radius=6
                )
                icon_container = ft.Container(
                    width=28, height=28,
                    bgcolor=ft.Colors.GREY_700,
                    border_radius=14
                )

                # クロージャ用にauthor情報をコピー
                _author = dict(author)

                def make_on_follow(au, fb, st):
                    def on_follow(e):
                        fb.disabled = True
                        page.update()
                        try:
                            client.follow_user(au['user_id'])
                            db.conn.execute(
                                "INSERT OR IGNORE INTO following_users (user_id, name, is_zipped) VALUES (?, ?, ?)",
                                (au['user_id'], au['user_name'],
                                 1 if db.get_setting("auto_archive_new_users", "0") == "1" else 0)
                            )
                            db.conn.commit()
                            st.value = "フォロー済"
                            st.color = ft.Colors.GREEN_400
                            fb.visible = False
                            append_log(f"フォローしました: {au['user_name']} ({au['user_id']})")
                            load_follow_list_ui()
                        except Exception as ex:
                            st.value = "失敗"
                            st.color = ft.Colors.RED_400
                            fb.disabled = False
                            append_log(f"フォロー失敗: {ex}")
                        page.update()
                    return on_follow

                follow_btn.on_click = make_on_follow(_author, follow_btn, status_text)

                row = ft.Row([
                    work_thumb_container,
                    ft.Column([
                        ft.Row([
                            icon_container,
                            ft.Text(f"{_author['user_name']} ({_author['user_id']})", weight=ft.FontWeight.BOLD)
                        ], spacing=8),
                        ft.Text(f"代表作: {_author.get('work_title', '')}", size=12, color=ft.Colors.GREY_400, width=280)
                    ], expand=True),
                    follow_btn,
                    status_text
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER)

                lv.controls.append(row)

                if _author.get('thumb_url'):
                    containers_to_load.append((_author['thumb_url'], work_thumb_container))
                if _author.get('profile_img_url'):
                    containers_to_load.append((_author['profile_img_url'], icon_container))

                # 5件ごとに画面更新
                if i % 5 == 0:
                    loading_text.value = f"{i + 1} / {len(authors)} 件を読み込み中..."
                    page.update()

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
                        container.content = ft.Image(
                            src=f"data:image/jpeg;base64,{b64}",
                            fit="cover"
                        )
                        updated_count += 1
                        # 4件ごと、あるいは最後の1件で画面更新してレスポンス向上
                        if updated_count % 4 == 0:
                            page.update()
                page.update()

        page.run_thread(load_authors)

    check_unfollowed_btn.on_click = lambda _: threading.Thread(target=run_check_unfollowed, daemon=True).start()

    tab_bookmark_content = ft.Column([
        ft.Text("ブックマーク一括ダウンロード", size=20, weight=ft.FontWeight.BOLD),
        ft.Text("あなたのブックマーク作品を一括保存し、保存フォルダに階層化またはフラット配置します。", size=12, color=ft.Colors.GREY_400),
        ft.Divider(),
        ft.Row([bm_target_type_dropdown, bm_rest_type_dropdown, bm_run_btn, check_unfollowed_btn, bm_pause_btn, bm_stop_btn], wrap=True),
        ft.Row([bm_progress_bar, bm_progress_text, bm_remaining_time_text]),
    ], expand=True)

    # --- タブ: 失敗キュー・品質異常リスト ---
    failed_list_view = ft.ListView(expand=True, spacing=5)

    def load_failed_queue_ui():
        failed_list_view.controls.clear()
        jobs = db.get_failed_jobs()
        if not jobs:
            failed_list_view.controls.append(
                ft.Container(
                    content=ft.Text("現在、失敗キューおよび品質異常の作品はありません ✅", color=ft.Colors.GREEN_300),
                    padding=20
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
                                client = PixivClient()
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
                            ft.Text(f"理由: {reason} | リトライ回数: {rc}", size=12, color=ft.Colors.ORANGE_300),
                        ], expand=True),
                        ft.ElevatedButton("再試行", icon=ft.Icons.REFRESH, on_click=make_retry_one()),
                        ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="削除", on_click=make_remove_one()),
                    ]),
                    bgcolor="#2A2D30",
                    padding=10,
                    border_radius=6
                )
                failed_list_view.controls.append(row_card)
        page.update()

    def on_retry_all_failed(e):
        jobs = db.get_failed_jobs()
        if not jobs:
            handle_alert("再試行対象の失敗作品がありません。")
            return
        append_log(f"失敗キュー全件再試行を開始します ({len(jobs)}件)...", ft.Colors.BLUE_300)
        def run_all_r():
            client = PixivClient()
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
            ft.Text("ダウンロード失敗キュー・保存品質異常リスト", size=20, weight=ft.FontWeight.BOLD),
            ft.IconButton(ft.Icons.REFRESH, tooltip="リスト更新", on_click=lambda _: load_failed_queue_ui()),
        ]),
        ft.Text("通信エラーや品質異常（0バイト/破損等）が発生した作品一覧です。再試行やCSVエクスポートが行えます。", size=12, color=ft.Colors.GREY_400),
        ft.Row([
            ft.ElevatedButton("全件再試行", icon=ft.Icons.PLAY_ARROW, color=ft.Colors.WHITE, bgcolor=ft.Colors.BLUE_600, on_click=on_retry_all_failed),
            ft.OutlinedButton("CSV出力", icon=ft.Icons.FILE_DOWNLOAD, on_click=on_export_failed_csv),
            ft.OutlinedButton("全件クリア", icon=ft.Icons.DELETE_OUTLINE, on_click=on_clear_all_failed),
        ]),
        ft.Divider(),
        failed_list_view
    ], expand=True)

    # --- タブ切り替え ---

    tab1_container = ft.Container(content=tab1_content, padding=10, visible=True)
    tab2_container = ft.Container(content=tab2_content, padding=10, visible=False, expand=True)
    tab_bookmark_container = ft.Container(content=tab_bookmark_content, padding=10, visible=False, expand=True)
    tab_queue_container = ft.Container(content=tab_queue_content, padding=10, visible=False, expand=True)
    tab_failed_container = ft.Container(content=tab_failed_content, padding=10, visible=False, expand=True)
    tab_extension_container = ft.Container(content=tab_extension_content, padding=10, visible=False)

    tab_containers = [tab1_container, tab2_container, tab_bookmark_container, tab_queue_container, tab_failed_container, tab_extension_container]
    tab_buttons = []
    
    def on_tab_change(idx):
        if idx == 99:
            open_save_folder()
            return

        for i, container in enumerate(tab_containers):
            container.visible = (i == idx)
        
        for btn in tab_buttons:
            if btn.data != 99:
                btn.style = ft.ButtonStyle(
                    color=ft.Colors.BLUE_400 if btn.data == idx else ft.Colors.GREY_400,
                    bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.BLUE) if btn.data == idx else ft.Colors.TRANSPARENT,
                )
        page.update()

    def create_tab_btn(idx, label, icon):
        btn = ft.TextButton(
            label, icon=icon,
            data=idx,
            on_click=lambda e, i=idx: on_tab_change(i),
            style=ft.ButtonStyle(
                color=ft.Colors.BLUE_400 if idx == 0 else ft.Colors.GREY_400,
                bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.BLUE) if idx == 0 else ft.Colors.TRANSPARENT,
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
                ft.Text("PixivVault", size=32, weight=ft.FontWeight.BOLD),
                login_status_text,
            ]),
            ft.Row([open_folder_btn, settings_btn], spacing=5)
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        custom_tab_bar,
        tab1_container,
        tab2_container,
        tab_bookmark_container,
        tab_queue_container,
        tab_failed_container,
        tab_extension_container,
        log_container
    )


    # --- 起動時バックグラウンド処理 ---
    def run_auto_sync_thread(my_id):
        try:
            client = PixivClient()
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

    def check_login_status():
        status_info = PixivClient.check_cookie_status("cookies.txt")
        st = status_info.get("status", "expired")
        msg = status_info.get("message", "")
        uid = status_info.get("user_id")

        if st == "valid":
            login_status_text.value = f"● [有効] {msg}"
            login_status_text.color = ft.Colors.GREEN_400
            if uid:
                db.set_setting("my_user_id", str(uid))
                trigger_auto_sync(uid)
        elif st == "warning":
            login_status_text.value = f"▲ [期限切れ間近] {msg}"
            login_status_text.color = ft.Colors.ORANGE_400
            if uid:
                db.set_setting("my_user_id", str(uid))
                trigger_auto_sync(uid)
        else:
            login_status_text.value = f"× [未認証/期限切れ] {msg}"
            login_status_text.color = ft.Colors.RED_400
        page.update()

    # 初期化
    gui_trigger_cookie_check[0] = lambda: threading.Thread(target=check_login_status, daemon=True).start()
    load_follow_list_ui()
    load_failed_queue_ui()
    threading.Thread(target=check_login_status, daemon=True).start()

