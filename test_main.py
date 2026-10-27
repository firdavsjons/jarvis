import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core.ai import LocalAIClient
from core.engine import SpeechEngine, pick_voice, say_command
from main import normalize_ai_response, parse_confirmation

SAY_VOICES = (
    "Albert              en_US    # Hello! My name is Albert.\n"
    "Eddy (Russian (Russia)) ru_RU    # Привет! Меня зовут Эдди.\n"
    "Milena              ru_RU    # Здравствуйте! Меня зовут Милена.\n"
    "Zarvox              en_US    # Hello! My name is Zarvox.\n"
)


class TestParseConfirmation(unittest.TestCase):
    def test_confirm(self):
        for text in ["ДА!", "да, выполняй", "Подтверждаю.", "выполняй пожалуйста"]:
            self.assertTrue(parse_confirmation(text), text)

    def test_cancel(self):
        for text in ["нет", "НЕТ, отмена", "отмена", "да... хотя нет"]:
            self.assertFalse(parse_confirmation(text), text)

    def test_unrecognized(self):
        for text in ["", "   ", "когда", "дано", "давай", "интернет", "наверное"]:
            self.assertIsNone(parse_confirmation(text), repr(text))
        print("✅ parse_confirmation: OK")


class TestNormalizeAIResponse(unittest.TestCase):
    def test_not_dict(self):
        for data in [None, "строка", ["response", "actions"], 42]:
            self.assertEqual(normalize_ai_response(data), ("", []), repr(data))

    def test_missing_keys(self):
        self.assertEqual(normalize_ai_response({}), ("", []))
        self.assertEqual(normalize_ai_response({"response": "Привет"}), ("Привет", []))

    def test_response_not_string(self):
        actions = [{"name": "open_app", "args": {"app": "Safari"}}]
        self.assertEqual(normalize_ai_response({"response": 5, "actions": actions}), ("", actions))
        self.assertEqual(normalize_ai_response({"response": None}), ("", []))

    def test_valid(self):
        data = {"response": "Ок", "actions": [{"name": "open_app", "args": {"app": "Safari"}}]}
        self.assertEqual(normalize_ai_response(data), ("Ок", data["actions"]))

    def test_bad_actions_become_empty_after_split(self):
        with patch('openai.OpenAI'):
            client = LocalAIClient()
        _, actions = normalize_ai_response({"response": "Ок", "actions": "rm -rf ~"})
        self.assertEqual(client.split_actions(actions), ([], []))
        print("✅ normalize_ai_response: OK")


class TestVoice(unittest.TestCase):
    def test_pick_voice_prefers_milena(self):
        self.assertEqual(pick_voice(SAY_VOICES), "Milena")

    def test_pick_voice_other_russian(self):
        output = SAY_VOICES.replace("Milena ", "Yuri   ")
        self.assertEqual(pick_voice(output), "Eddy (Russian (Russia))")

    def test_pick_voice_no_russian(self):
        self.assertIsNone(pick_voice("Albert              en_US    # Hello!\n"))
        self.assertIsNone(pick_voice(""))

    def test_text_after_double_dash(self):
        text = "-v Zarvox привет"
        args = say_command(text, "Milena")
        self.assertEqual(args, ["say", "-v", "Milena", "--", "-v Zarvox привет"])
        self.assertEqual(args.index("--") + 1, args.index(text))
        self.assertEqual(args[-1], text)
        self.assertEqual(say_command(text, None), ["say", "--", text])
        print("✅ say: текст после '--': OK")


@patch('core.engine._detect_voice', return_value="Milena")
@patch('core.engine.sr.Microphone')
class TestSpeechEngine(unittest.TestCase):
    @patch('core.engine.subprocess.Popen')
    def test_speak_passes_text_after_double_dash(self, mock_popen, *_):
        engine = SpeechEngine()
        engine.speak("-v Zarvox привет")

        mock_popen.assert_called_once_with(["say", "-v", "Milena", "--", "-v Zarvox привет"])
        self.assertFalse(engine.speaking.is_set())
        self.assertTrue(engine.speech_overlaps(0))

    @patch('core.engine.subprocess.Popen')
    def test_stop_speaking_terminates_say(self, mock_popen, *_):
        engine = SpeechEngine()
        proc = MagicMock()
        proc.poll.return_value = None  # процесс ещё говорит
        engine._proc = proc

        engine.stop_speaking()

        proc.terminate.assert_called_once()

    @patch('core.engine.subprocess.Popen')
    def test_queued_phrase_cancelled_after_stop(self, mock_popen, *_):
        engine = SpeechEngine()
        release = threading.Event()
        mock_popen.return_value.wait.side_effect = lambda: release.wait(2)  # «say» говорит, пока не отпустим
        mock_popen.return_value.poll.return_value = None

        first = threading.Thread(target=engine.speak, args=("первая",))
        first.start()
        self.assertTrue(engine.speaking.wait(2))
        second = threading.Thread(target=engine.speak, args=("вторая",))  # ждёт Lock
        second.start()
        time.sleep(0.05)

        engine.stop_speaking()
        release.set()
        first.join(2)
        second.join(2)

        mock_popen.return_value.terminate.assert_called_once()
        self.assertEqual(mock_popen.call_count, 1)  # вторая фраза не прозвучала
        print("✅ SpeechEngine: OK")


if __name__ == '__main__':
    unittest.main()
