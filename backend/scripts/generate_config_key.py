"""生成可直接写入 .env 的 32 字节（256-bit）URL-safe Base64 运行时配置加密主密钥。"""

import base64
import secrets
import sys


def generate_config_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def main() -> int:
    if len(sys.argv) != 1:
        print("本工具不接受参数，直接执行即可生成新的配置加密密钥。", file=sys.stderr)
        return 2
    print(f"PDFTR_CONFIG_SECRET_KEY={generate_config_key()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
