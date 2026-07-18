from PIL import Image, ImageDraw
import os
import sys
import logging

def get_asset_path(filename):
    if getattr(sys, 'frozen', False):
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
        path = os.path.join(base_path, 'assets', filename)
        if not os.path.exists(path):
            path = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'assets', filename)
        return path
    return os.path.join(os.path.abspath("."), 'assets', filename)

def create_image():
    # assets/icon.png をタスクトレイアイコンとして読み込む
    icon_path = get_asset_path("icon.png")
    if os.path.exists(icon_path):
        try:
            with Image.open(icon_path) as img:
                return img.copy()  # ファイルハンドルを即座に閉じるためcopy()で返す
        except Exception:
            pass


    # フォールバック: ダミーの青い正方形（簡易的なダミーアイコン）
    image = Image.new('RGB', (64, 64), color = (52, 152, 219))
    dc = ImageDraw.Draw(image)
    dc.rectangle((16, 16, 48, 48), fill=(41, 128, 185))
    return image

def _create_pystray_icon_macos(pystray, name, image, title, menu):
    """pystray.Icon()のコンストラクタはmacOS版内部でNSStatusBar.statusItemWithLength_
    等のAppKitオブジェクトを直接生成するため、OSのメインスレッド以外から呼ぶと
    'NSWindow should only be instantiated on the main thread!' で例外になる。

    PyInstallerビルドではPythonの起動スレッド自体がOSのメインスレッドと一致するため
    問題は起きないが、flet buildで生成したアプリはPythonコードがFlutterランナーの
    別スレッド上で動くため、pystray.Icon()の生成だけメインスレッドへ同期ディスパッチする。
    """
    try:
        from Foundation import NSThread, NSObject
    except Exception:
        # PyObjCが使えない環境ではそのまま直接生成を試みる
        return pystray.Icon(name, image, title, menu)

    if NSThread.isMainThread():
        return pystray.Icon(name, image, title, menu)

    class _MainThreadIconCreator(NSObject):
        def createIcon_(self, _arg):
            try:
                self.icon = pystray.Icon(name, image, title, menu)
            except Exception as e:
                self.error = e

    creator = _MainThreadIconCreator.alloc().init()
    creator.performSelectorOnMainThread_withObject_waitUntilDone_(
        "createIcon:", None, True
    )
    if getattr(creator, "error", None) is not None:
        raise creator.error
    return creator.icon


def run_tray(on_show_clicked, on_exit_clicked):
    try:
        import pystray
    except Exception as e:
        logging.getLogger(__name__).warning(f"システムトレイの初期化に失敗しました。この環境ではサポートされていない可能性があります。: {e}")
        return None

    def on_show(icon, item):
        on_show_clicked()

    def on_exit(icon, item):
        icon.stop()
        on_exit_clicked()

    menu = pystray.Menu(
        pystray.MenuItem('PixivVaultを開く', on_show, default=True),
        pystray.MenuItem('終了', on_exit)
    )

    image = create_image()
    try:
        if sys.platform == "darwin":
            icon = _create_pystray_icon_macos(pystray, "PixivVault", image, "PixivVault", menu)
        else:
            icon = pystray.Icon("PixivVault", image, "PixivVault", menu)
    except Exception as e:
        logging.getLogger(__name__).warning(f"システムトレイアイコンの生成に失敗しました。この機能はスキップされます。: {e}")
        return None

    icon.run_detached()
    return icon
