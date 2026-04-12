"""
WeChat Decrypt 一键启动

python main.py          # 提取密钥 + 启动 Web UI
python main.py decrypt  # 提取密钥 + 解密全部数据库
"""
import json
import os
import sys

import functools
print = functools.partial(print, flush=True)

from key_utils import strip_key_metadata
from service_runtime import (
    SERVICE_EXIT_ALREADY_RUNNING,
    SERVICE_EXIT_PORT_CONFLICT,
    ServiceInstanceGuard,
    build_port_conflict_message,
    build_service_already_running_message,
    probe_local_service_state,
)
from wechat_status import is_wechat_running


def check_wechat_running(process_name=None):
    """检查微信是否在运行，返回 True/False"""
    return is_wechat_running(process_name)


def ensure_keys(keys_file, db_dir):
    """确保密钥文件存在且匹配当前 db_dir，否则重新提取"""
    if os.path.exists(keys_file):
        try:
            with open(keys_file, encoding="utf-8") as f:
                keys = json.load(f)
        except (json.JSONDecodeError, ValueError):
            keys = {}
        # 检查密钥是否匹配当前 db_dir（防止切换账号后误复用旧密钥）
        saved_dir = keys.pop("_db_dir", None)
        if saved_dir and os.path.normcase(os.path.normpath(saved_dir)) != os.path.normcase(os.path.normpath(db_dir)):
            print(f"[!] 密钥文件对应的目录已变更，需要重新提取")
            print(f"    旧: {saved_dir}")
            print(f"    新: {db_dir}")
            keys = {}
        keys = strip_key_metadata(keys)
        if keys:
            print(f"[+] 已有 {len(keys)} 个数据库密钥")
            return

    print("[*] 密钥文件不存在，正在从微信进程提取...")
    print()
    from find_all_keys import main as extract_keys
    try:
        extract_keys()
    except RuntimeError as e:
        print(f"\n[!] 密钥提取失败: {e}")
        sys.exit(1)
    print()

    # 提取后再次检查
    if not os.path.exists(keys_file):
        print("[!] 密钥提取失败")
        sys.exit(1)
    try:
        with open(keys_file, encoding="utf-8") as f:
            keys = json.load(f)
    except (json.JSONDecodeError, ValueError):
        keys = {}
    if not strip_key_metadata(keys):
        print("[!] 未能提取到任何密钥")
        print("    可能原因：选择了错误的微信数据目录，或微信需要重启")
        print("    请检查 config.json 中的 db_dir 是否与当前登录的微信账号匹配")
        sys.exit(1)


def main():
    try:
        from config import get_config_path
        from log_utils import init_app_logging

        _, log_path = init_app_logging("service", config_path=get_config_path())
        print(f"[log] {log_path}")
    except Exception:
        pass

    print("=" * 60)
    print("  WeChat Decrypt")
    print("=" * 60)
    print()

    # 1. 加载配置（自动检测 db_dir）
    from config import load_config
    cfg = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "web"

    service_guard = None
    if cmd == "web":
        state = probe_local_service_state(cfg.get("listen_host"), cfg.get("listen_port", 5678))
        if state == "service":
            print(f"[!] {build_service_already_running_message(cfg.get('listen_host'), cfg.get('listen_port', 5678))}")
            sys.exit(SERVICE_EXIT_ALREADY_RUNNING)
        if state == "occupied":
            print(f"[!] {build_port_conflict_message(cfg.get('listen_host'), cfg.get('listen_port', 5678))}")
            sys.exit(SERVICE_EXIT_PORT_CONFLICT)

        service_guard = ServiceInstanceGuard(
            cfg.get("listen_host"),
            cfg.get("listen_port", 5678),
            config_path=os.environ.get("WECHAT_DECRYPT_CONFIG", ""),
        )
        if not service_guard.acquire():
            print(f"[!] {build_service_already_running_message(cfg.get('listen_host'), cfg.get('listen_port', 5678))}")
            sys.exit(SERVICE_EXIT_ALREADY_RUNNING)

    try:
        # 2. 检查微信进程
        if not check_wechat_running(cfg.get("wechat_process")):
            print(f"[!] 未检测到微信进程 ({cfg.get('wechat_process', 'WeChat')})")
            print("    请先启动微信并登录，然后重新运行")
            sys.exit(1)
        print("[+] 微信进程运行中")

        # 3. 提取密钥
        ensure_keys(cfg["keys_file"], cfg["db_dir"])

        # 4. 根据子命令执行
        if cmd == "decrypt":
            print("[*] 开始解密全部数据库...")
            print()
            from decrypt_db import main as decrypt_all
            decrypt_all()
        elif cmd == "web":
            print("[*] 启动 Web UI...")
            print()
            from monitor_web import main as start_web
            start_web()
        else:
            print(f"[!] 未知命令: {cmd}")
            print()
            print("用法:")
            print("  python main.py          启动实时消息监听 (Web UI)")
            print("  python main.py decrypt  解密全部数据库到 decrypted/")
            sys.exit(1)
    finally:
        if service_guard is not None:
            service_guard.release()


if __name__ == "__main__":
    main()
