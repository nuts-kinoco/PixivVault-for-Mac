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
            return Image.open(icon_path)
        except Exception:
            pass
    
    # フォールバック: ダミーの青い正方形（簡易的なダミーアイコン）
    image = Image.new('RGB', (64, 64), color = (52, 152, 219))
    dc = ImageDraw.Draw(image)
    dc.rectangle((16, 16, 48, 48), fill=(41, 128, 185))
    return image

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

    icon = pystray.Icon("PixivVault", create_image(), "PixivVault", menu)
    icon.run_detached()
    return icon
