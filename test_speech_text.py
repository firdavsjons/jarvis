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

    def test_path_with_spaces(self):
        self.assertEqual(prepare_for_speech("Сохранил в /Users/x/Projects All/a.txt."),
                         "Сохранил в a точка тэ-экс-тэ, из папки Projects All.")
        self.assertEqual(prepare_for_speech("Файл /Users/x/Desktop/мой отчёт.pdf готов"),
                         "Файл мой отчёт точка пэ-дэ-эф, с рабочего стола готов")
        self.assertEqual(prepare_for_speech("Файлы /Users/x/Desktop/a.txt и /Users/x/Downloads/b.pdf"),
                         "Файлы a точка тэ-экс-тэ, с рабочего стола и b точка пэ-дэ-эф, из загрузок")

    def test_file_name_extension(self):
        self.assertEqual(prepare_for_speech("Открой test.txt"), "Открой test точка тэ-экс-тэ")
        self.assertEqual(prepare_for_speech("Файл report.PDF готов."), "Файл report точка пэ-дэ-эф готов.")
        # Не имя файла: домен без схемы, число
        self.assertEqual(prepare_for_speech("Сайт youtube.com, версия 3.5"), "Сайт youtube.com, версия 3.5")

    def test_underscore_inside_names_kept(self):
        self.assertEqual(prepare_for_speech("Переменная snake_case и my_file"), "Переменная snake_case и my_file")
        self.assertEqual(prepare_for_speech("Файл my_file.py"), "Файл my_file точка пэ-игрек")
        self.assertEqual(prepare_for_speech("Это _важно_ и __очень__"), "Это важно и очень")

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


class TestUnits(unittest.TestCase):
    def test_weather_line(self):
        self.assertEqual(
            prepare_for_speech("Душанбе: сейчас +18°C, ощущается как +16°C, ветер 3 м/с. Сегодня от -2°C до +21°C."),
            "Душанбе: сейчас плюс 18 градусов Цельсия, ощущается как плюс 16 градусов Цельсия, ветер 3 метра в секунду. "
            "Сегодня от минус 2 градусов Цельсия до плюс 21 градуса Цельсия.")

    def test_agreement(self):
        cases = [("1°C", "1 градус Цельсия"), ("2°C", "2 градуса Цельсия"), ("11°C", "11 градусов Цельсия"),
                 ("21°C", "21 градус Цельсия"), ("2,5°C", "2,5 градуса Цельсия"), ("41%", "41 процент"),
                 ("12%", "12 процентов"), ("1 м/с", "1 метр в секунду"), ("5 м/с", "5 метров в секунду"),
                 ("0,5 мм", "0,5 миллиметра")]
        for text, expected in cases:
            self.assertEqual(prepare_for_speech(text), expected, text)

    def test_currencies(self):
        self.assertEqual(prepare_for_speech("1 USD = 10,93 TJS"), "1 доллар равно 10,93 сомони")
        self.assertEqual(prepare_for_speech("$5 и 100 ₽, 3 EUR, 2 CNY, 7 сомони"),
                         "5 долларов и 100 рублей, 3 евро, 2 юаня, 7 сомони")

    def test_plain_degrees_and_numbers_unchanged(self):
        for text in ["Сейчас 25° тепла.", "Версия 3.5", "Время 12:30"]:
            self.assertEqual(prepare_for_speech(text), text)
        print("✅ Числа и единицы: OK")


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
