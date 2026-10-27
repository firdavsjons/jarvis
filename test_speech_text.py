import os
import sys
import unittest

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core.speech_text import confirmation_question, domain_for_speech, prepare_for_speech


class TestPrepareForSpeech(unittest.TestCase):
    def test_url_with_path_and_params(self):
        self.assertEqual(prepare_for_speech("Открываю https://youtube.com/watch?v=abc123&t=10s для вас."),
                         "Открываю ютуб точка ком для вас.")
        self.assertEqual(prepare_for_speech("Код тут: https://github.com/user/repo/blob/main/a.py#L10"),
                         "Код тут: гитхаб точка ком")

    def test_url_with_www(self):
        self.assertEqual(prepare_for_speech("Открыл https://www.google.com/search?q=погода."),
                         "Открыл гугл точка ком.")
        self.assertEqual(domain_for_speech("www.ru.wikipedia.org"), "ру точка википедия точка орг")
        self.assertEqual(prepare_for_speech("Сайт http://example.com"), "Сайт example точка ком")

    def test_no_technical_words(self):
        spoken = prepare_for_speech("См. https://www.youtube.com/a/b/c?x=1 и http://github.com/")
        for word in ["http", "https", "www", "/", "слеш", "?", "="]:
            self.assertNotIn(word, spoken)

    def test_desktop_path(self):
        self.assertEqual(prepare_for_speech("Переместил /Users/test/Desktop/отчёт.txt в Корзину."),
                         "Переместил отчёт точка тэ-экс-тэ, с рабочего стола в Корзину.")
        self.assertEqual(prepare_for_speech("Файл ~/Downloads/счёт.pdf"),
                         "Файл счёт точка пэ-дэ-эф, из загрузок")
        self.assertEqual(prepare_for_speech("Файл /Users/test/Documents/план.docx готов"),
                         "Файл план точка ворд, из документов готов")

    def test_markdown(self):
        self.assertEqual(prepare_for_speech("**Готово!** Вот `код` и # заголовок, _курсив_."),
                         "Готово! Вот код и заголовок, курсив.")
        self.assertEqual(prepare_for_speech("Читайте [Википедию](https://ru.wikipedia.org/wiki/X)."),
                         "Читайте Википедию.")
        self.assertEqual(prepare_for_speech("Подробнее на сайте (https://example.com/a)."),
                         "Подробнее на сайте.")

    def test_emoji(self):
        self.assertEqual(prepare_for_speech("✅ Готово 🚀"), "Готово")
        self.assertEqual(prepare_for_speech("Привет 👋🏽! Всё ок 👍"), "Привет! Всё ок")

    def test_long_symbol_chains(self):
        self.assertEqual(prepare_for_speech("Итог ======== конец --> дальше"), "Итог конец дальше")
        self.assertEqual(prepare_for_speech("id a1b2c3d4e5f6a7b8c9d0 сохранён"), "id сохранён")

    def test_plain_text_unchanged(self):
        for text in ["Привет! Чем могу помочь?", "Открываю Safari и ставлю громкость 50.",
                     "Сейчас 25° тепла, это № 1 по погоде.", "Сделал громче", "-v Zarvox привет",
                     "Время: 12:30, а 1/2 — это половина.", "Ну что, да или нет?"]:
            with self.subTest(text=text):
                self.assertEqual(prepare_for_speech(text), text)

    def test_empty(self):
        self.assertEqual(prepare_for_speech(""), "")
        self.assertEqual(prepare_for_speech(None), "")
        self.assertEqual(prepare_for_speech("🚀✅"), "")


class TestConfirmationQuestion(unittest.TestCase):
    def test_short_questions(self):
        self.assertEqual(confirmation_question([{"name": "open_url", "args": {"url": "https://www.youtube.com/watch?v=1"}}]),
                         "Открыть сайт ютуб точка ком?")
        self.assertEqual(confirmation_question([{"name": "trash_file", "args": {"path": "/Users/test/Desktop/отчёт.txt"}}]),
                         "Переместить в Корзину файл отчёт с рабочего стола?")
        self.assertEqual(confirmation_question([{"name": "quit_app", "args": {"app": "Terminal"}}]),
                         "Закрыть Terminal?")

    def test_several(self):
        question = confirmation_question([
            {"name": "quit_app", "args": {"app": "Terminal"}},
            {"name": "open_url", "args": {"url": "https://evil.ru/x?token=1"}},
        ])
        self.assertEqual(question, "Нужно подтверждение: закрыть Terminal, открыть сайт evil точка ру. Выполнить?")
        print("✅ prepare_for_speech и вопросы: OK")


if __name__ == '__main__':
    unittest.main()
