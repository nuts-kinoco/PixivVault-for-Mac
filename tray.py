import pystray
from PIL import Image, ImageDraw
import os
import sys

def get_asset_path(filename):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, 'assets', filename)
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
