"""Общий для тестов список «установленных» приложений: тесты не зависят от того, что стоит на этом Mac."""
from unittest.mock import patch

FAKE_INSTALLED = [
    "Calculator", "Finder", "Google Chrome", "Mail", "Microsoft Excel", "Microsoft Word", "Music", "Notes",
    "PyCharm", "Safari", "System Settings", "Telegram", "Terminal", "Visual Studio Code", "Xcode", "YouTube",
]


def patch_installed(apps=FAKE_INSTALLED):
    return patch("core.installed_apps.list_installed_apps", return_value=list(apps))
