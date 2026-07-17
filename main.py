import flet as ft
from gui import main_window
from tray import run_tray
from database import Database
from scheduler import Scheduler
import sys
import os
import logging
from logging.handlers import RotatingFileHandler

# カスタムURIスキーム経由で起動された際、CwdがSystem32等になるのを防ぐため、
# 実行ファイル(またはスクリプト)が存在するフォルダにカレントディレクトリを強制変更します。
if getattr(sys, 'frozen', False):
    app_path = os.path.dirname(os.path.abspath(sys.executable))
    if sys.platform == "darwin" and "Contents/MacOS" in app_path:
        # macOSの.appバンドル内から起動された場合、リソースフォルダは書き込み適さないため、
        # ユーザーのApplication Support配下に作業ディレクトリを変更します。
        app_path = os.path.expanduser("~/Library/Application Support/PixivVault")
        os.makedirs(app_path, exist_ok=True)
else:
    app_path = os.path.dirname(os.path.abspath(sys.argv[0]))
os.chdir(app_path)


def setup_logging():
    """gui.py/server.py/core.py 等の logger.error()/logger.exception() が実際にファイルへ
    残るよう、アプリ全体で共有するファイルハンドラをここで一元設定する。
    サイズ上限付きのローテーションにより gui_error_log.txt が無制限に肥大化するのを防ぐ。
    """
    handler = RotatingFileHandler("gui_error_log.txt", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING)
    root_logger.addHandler(handler)


def ensure_branded_flet_desktop_client():
    """macOSでは、Flet本体が「Flet」という汎用ブランド(メニューバー表示名・Dock/Finderアイコン)の
    共有デスクトップクライアントを ~/.flet/client/ 配下にダウンロード/キャッシュして使う。
    このキャッシュはFletの同一バージョンを使う全アプリで共有されるため、PixivVault用に
    Info.plist(CFBundleName)とアイコン(AppIcon.icns)を起動のたびに冪等に上書きパッチする。
    """
    if sys.platform != "darwin":
        return
    try:
        import flet_desktop
        cache_dir = str(flet_desktop.ensure_client_cached())
        app_bundle = os.path.join(cache_dir, "Flet.app")
        marker = os.path.join(app_bundle, ".pixivvault_branded")
        if not os.path.isdir(app_bundle) or os.path.exists(marker):
            return

        if getattr(sys, 'frozen', False):
            # onefile: sys._MEIPASS, onedir: sys._MEIPASS or executable dir
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
            icon_src = os.path.join(base_path, 'assets', 'icon.icns')
            # If not found in _MEIPASS (onedir can have assets next to executable)
            if not os.path.exists(icon_src):
                icon_src = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'assets', 'icon.icns')
        else:
            icon_src = os.path.join(os.path.abspath("."), 'assets', 'icon.icns')

        import subprocess
        plist_path = os.path.join(app_bundle, "Contents", "Info.plist")
        subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Set :CFBundleName PixivVault", plist_path],
            check=False, capture_output=True,
        )

        icon_dst = os.path.join(app_bundle, "Contents", "Resources", "AppIcon.icns")
        if os.path.exists(icon_src) and os.path.isdir(os.path.dirname(icon_dst)):
            import shutil
            shutil.copyfile(icon_src, icon_dst)

        subprocess.run(["touch", app_bundle], check=False, capture_output=True)
        open(marker, "w").close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Fletクライアントのブランディング適用に失敗しました: {e}")


_instance_lock_socket = None

def check_single_instance(port=25011):
    import socket
    import sys
    global _instance_lock_socket
    try:
        _instance_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Windowsでは排他利用を設定して二重起動を防ぎ、他OSではREUSEADDRを使用する
        if sys.platform == 'win32':
            _instance_lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            _instance_lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _instance_lock_socket.bind(('127.0.0.1', port))
        _instance_lock_socket.listen(1)
    except socket.error:
        try:
            from notifications import send_notification
            send_notification("PixivVault", "PixivVaultは既に起動しています。")
        except Exception:
            pass
        sys.exit(0)

def main():
    setup_logging()
    check_single_instance()
    db = Database()
    
    # スケジューラの初期化と開始
    scheduler = Scheduler(db, log_callback=None)
    scheduler.start()
    
    # 拡張機能連携用ローカルサーバーの起動
    from pixiv_client import PixivClient
    from server import start_server
    import threading
    
    client = PixivClient()
    server_thread = threading.Thread(target=start_server, args=(25010, db, client), daemon=True)
    server_thread.start()

    import sys
    def get_asset_path(filename):
        if getattr(sys, 'frozen', False):
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
            path = os.path.join(base_path, 'assets', filename)
            if not os.path.exists(path):
                path = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'assets', filename)
            return path
        return os.path.join(os.path.abspath("."), 'assets', filename)

    def app_target(page: ft.Page):
        # 画面サイズの設定 (16:9比率)
        page.window.width = 800
        page.window.height = 600
        # ウィンドウのタイトルバー・タスクバーに適用するアイコンアセットを指定
        page.window.icon = get_asset_path("icon.ico" if sys.platform == "win32" else "icon.png")

        # ウィンドウのリサイズ制限（最大化・最小化のみ許可）
        page.window.resizable = False

        # メインUIの構築
        main_window(page)
        
        # ウィンドウの「×」ボタンで閉じないようにする
        page.window.prevent_close = True
        
        _tray_icon = None

        def on_window_event(e):
            if e.type == ft.WindowEventType.CLOSE:
                import gui
                is_downloading = gui.is_downloading_active[0]
                
                msg = "ダウンロードが実行中です。アプリを終了しますか？" if is_downloading else "アプリを終了します。よろしいですか？"
                
                def handle_yes(e):
                    page.pop_dialog()
                    if is_downloading and gui.request_stop_all[0]:
                        gui.request_stop_all[0]()
                    
                    scheduler.stop()
                    try:
                        if _tray_icon:
                            _tray_icon.stop()
                    except Exception:
                        pass
                    page.run_task(page.window.destroy)

                def handle_no(e):
                    page.pop_dialog()

                confirm_dialog = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("終了確認"),
                    content=ft.Text(msg),
                    actions=[
                        ft.TextButton("はい", on_click=handle_yes),
                        ft.TextButton("いいえ", on_click=handle_no),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                page.show_dialog(confirm_dialog)
            elif e.type == ft.WindowEventType.MINIMIZE:
                # トレイアイコンが利用できない環境(pystray初期化失敗)では、最小化で隠すと
                # ウィンドウを復元する手段が無くなり操作不能になるため、_tray_icon がある場合のみ隠す。
                if _tray_icon and db.get_setting("minimize_to_tray", "1") == "1":
                    page.window.visible = False
                    page.update()
        
        page.window.on_event = on_window_event

        # タスクトレイのアクション
        def on_show_clicked():
            page.window.visible = True
            page.window.to_front()
            page.update()

        def on_exit_clicked():
            # アプリの完全終了
            scheduler.stop()
            page.run_task(page.window.destroy)

        # トレイアイコンをバックグラウンドで開始
        _tray_icon = run_tray(on_show_clicked, on_exit_clicked)

    ensure_branded_flet_desktop_client()
    ft.run(app_target, assets_dir="assets")

if __name__ == '__main__':
    main()
