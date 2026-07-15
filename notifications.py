import sys
import subprocess
import logging

logger = logging.getLogger(__name__)


def send_notification(title: str, message: str, app_id: str = "PixivVault") -> None:
    """OS通知(トースト)を送信する。
    Windows: win11toast / macOS: osascript(display notification) を使用し、
    それ以外のOSでは何もしない(ログ出力のみ)。
    """
    if sys.platform == "win32":
        try:
            from win11toast import toast
            toast(title, message, app_id=app_id)
        except Exception as e:
            logger.error(f"通知の送信に失敗しました(Windows): {e}")
    elif sys.platform == "darwin":
        script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
        try:
            subprocess.run(
                ["osascript", "-e", script, title, message],
                check=True,
                timeout=5,
                capture_output=True,
            )
        except Exception as e:
            logger.error(f"通知の送信に失敗しました(macOS): {e}")
    else:
        logger.warning(f"通知未対応のOSのためスキップしました: {sys.platform}")
