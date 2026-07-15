import sys
import os
import logging

logger = logging.getLogger(__name__)

# Windows以外ではwinregをロードしない(モジュール自体が存在しないため)
if sys.platform == "win32":
    import winreg
else:
    winreg = None

REG_PATH = r"Software\Classes\pixivvault"


def get_executable_command() -> str:
    """現在の環境(開発/ビルド済)に合わせた起動コマンド文字列を返します"""
    if getattr(sys, 'frozen', False):
        # PyInstaller等でビルドされたEXEの場合
        exe_path = os.path.abspath(sys.executable)
        return f'"{exe_path}" "%1"'
    else:
        # 開発用 python 環境の場合
        python_exe = os.path.abspath(sys.executable)
        main_py = os.path.abspath(sys.argv[0])
        return f'"{python_exe}" "{main_py}" "%1"'


def register_protocol() -> bool:
    """カスタムURIスキーム(pixivvault://)をレジストリに登録します(Windows専用)。
    macOS/Linuxでは何もせず成功扱いを返す(URLスキームはInfo.plistで解決済みのため)。
    """
    if sys.platform != "win32":
        logger.info("このOSではレジストリ登録は不要です(スキップ)。")
        return True

    command = get_executable_command()
    try:
        # HKEY_CURRENT_USER\Software\Classes\pixivvault を作成
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:PixivVault Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

            # shell\open\command キーを作成
            with winreg.CreateKey(key, r"shell\open\command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)

        logger.info(f"レジストリに登録しました: {command}")
        return True
    except Exception as e:
        logger.error(f"レジストリの登録に失敗しました: {e}")
        return False


def unregister_protocol() -> bool:
    """カスタムURIスキームのレジストリ登録を解除します(Windows専用)。"""
    if sys.platform != "win32":
        return True

    try:
        # サブキーから順番に削除する必要がある
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell\open\command")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell\open")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_PATH)
        logger.info("レジストリから登録を解除しました。")
        return True
    except FileNotFoundError:
        # 既に存在しない場合は成功とみなす
        return True
    except Exception as e:
        # コマンドが見つからない等で不完全なツリーの場合のフォールバック
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_PATH)
            return True
        except Exception:
            pass
        logger.error(f"レジストリの解除に失敗しました: {e}")
        return False


def check_protocol_registered() -> bool:
    """現在カスタムURIスキームが登録されているか確認します(Windows専用)。
    macOS/Linuxでは常にFalseを返す(GUI側はレジストリ操作ボタンをWindows限定表示にする想定)。
    """
    if sys.platform != "win32":
        return False

    expected_command = get_executable_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{REG_PATH}\shell\open\command") as cmd_key:
            command, _ = winreg.QueryValueEx(cmd_key, "")
            return command == expected_command
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.error(f"レジストリの確認に失敗しました: {e}")
        return False
