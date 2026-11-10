"""隐藏输入生成管理密码 scrypt 哈希，不接受含明文密码的命令行参数。"""

import getpass
import sys
import warnings

from pdftr.domain.admin import hash_password


def main() -> int:
    if len(sys.argv) != 1:
        print("本工具不接受参数，请通过隐藏输入提供管理密码。", file=sys.stderr)
        return 2
    try:
        with warnings.catch_warnings():
            # 无法关闭回显时终止，不能让 getpass 自动回退到普通输入。
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("管理密码（至少 16 字符，最多 256 UTF-8 字节）：")
            confirmation = getpass.getpass("再次输入管理密码：")
        if password != confirmation:
            print("两次输入的密码不一致。", file=sys.stderr)
            return 2
        print(hash_password(password))
    except (EOFError, KeyboardInterrupt):
        print("已取消。", file=sys.stderr)
        return 2
    except getpass.GetPassWarning:
        print("无法隐藏输入，请在本机交互终端中执行。", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
