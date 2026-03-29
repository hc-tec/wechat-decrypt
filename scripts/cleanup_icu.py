import glob
import os
import sys
import time


def _disable_icu_dlls_in_dir(dir_path: str, *, size_threshold: int = 200_000) -> list[str]:
    dir_path = os.path.abspath(os.path.expanduser(dir_path or ""))
    if not dir_path or not os.path.isdir(dir_path):
        return []

    disabled: list[str] = []
    for p in glob.glob(os.path.join(dir_path, "icu*.dll")):
        try:
            if not os.path.isfile(p):
                continue
            if os.path.isfile(p + ".disabled"):
                continue
            if os.path.getsize(p) < int(size_threshold):
                continue

            dst = p + ".disabled"
            # 在 Windows 上，刚产出的 DLL 可能短暂被 Defender/索引器占用，
            # 这里做有限重试，避免构建脚本偶发“清理无效”。
            success = False
            for attempt in range(1, 8):
                try:
                    os.replace(p, dst)
                    disabled.append(os.path.basename(p))
                    success = True
                    break
                except OSError:
                    if attempt >= 7:
                        break
                    time.sleep(0.15 * attempt)
            if not success and os.path.isfile(p) and (not os.path.isfile(dst)):
                try:
                    print(f"[cleanup] WARN: failed to disable {os.path.basename(p)} (file locked?)", file=sys.stderr, flush=True)
                except Exception:
                    pass
        except Exception:
            continue
    return disabled


def main(argv: list[str]) -> int:
    # Default: try common PyInstaller onedir layouts.
    dirs = argv[1:] or [
        os.path.join("dist", "WeChatDataServiceGUI", "_internal"),
        os.path.join("dist", "WeChatDataServiceGUIConsole", "_internal"),
    ]

    any_changed = False
    for d in dirs:
        disabled = _disable_icu_dlls_in_dir(d)
        if disabled:
            any_changed = True
            print(f"[cleanup] {d}: disabled {', '.join(disabled)}", flush=True)

    return 0 if any_changed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
