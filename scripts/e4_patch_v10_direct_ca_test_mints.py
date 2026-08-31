#!/usr/bin/env python3
from pathlib import Path


def main() -> int:
    path = Path("tests/test_e4_v10_direct_ca_social.py")
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "9xQeWvG816bUx9EPfEZn9Y6b7nWn1N5uVxkZ7L1pump",
        "Aijysj19Tv4yYFvUunHQRqpkVDggU9GNUFJpYaetpump",
    )
    text = text.replace(
        "8xQeWvG816bUx9EPfEZn9Y6b7nWn1N5uVxkZ7L2pump",
        "GzVhofvBXc4kFSLF8Ndw26QN14WAPKiKiGc7WbcCpump",
    )
    path.write_text(text, encoding="utf-8")
    print("patched direct-CA tests with valid Solana pubkeys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
