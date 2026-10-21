# 供 `<python> -m mihomo_cli` 使用：sudo 自调用与 systemd 单元都走这条路径，
# 不依赖 ~/.local/bin 是否在 sudo 的 secure_path 里（方案 5.1.7「sudo 的使用方式」）。
from mihomo_cli.cli import main

if __name__ == "__main__":
    main()
