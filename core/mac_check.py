"""Проверка чтения системы: python -m core.mac_check. Только чтение, ничего не меняет."""
import sys

from core.errors import ActionError
from core.mac_actions import SYSTEM_INFO_VALUES, system_info


def main():
    failed = 0
    for what in SYSTEM_INFO_VALUES:
        try:
            print(f"{what:14} {system_info(what).message}")
        except ActionError as e:
            failed += 1
            print(f"{what:14} ОШИБКА: {e.short}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
