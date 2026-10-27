import subprocess


def _run(args):
    """Запускает команду списком аргументов, без shell."""
    try:
        subprocess.run(args, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"❌ Ошибка команды {args}: {e}")


class MacControl:
    def execute(self, command):
        cmd = command.lower()

        if "громче" in cmd:
            # AppleScript: set volume output volume (current + 10)
            _run(["osascript", "-e", "set volume output volume (output volume of (get volume settings) + 10)"])
            return "Сделал громче"

        elif "тише" in cmd:
            _run(["osascript", "-e", "set volume output volume (output volume of (get volume settings) - 10)"])
            return "Сделал тише"

        elif "выключи звук" in cmd:
            _run(["osascript", "-e", "set volume output muted true"])
            return "Звук выключен"

        elif "включи звук" in cmd:
            _run(["osascript", "-e", "set volume output muted false"])
            return "Звук включен"

        elif "браузер" in cmd or "интернет" in cmd:
            # open с URL открывает браузер по умолчанию
            _run(["open", "https://www.google.com"])
            return "Открываю браузер"

        elif "спотифай" in cmd or "музык" in cmd:
            _run(["open", "-a", "Spotify"])
            return "Открываю музыку"

        elif "калькулятор" in cmd:
            _run(["open", "-a", "Calculator"])
            return "Калькулятор открыт"

        return None
