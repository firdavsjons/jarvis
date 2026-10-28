import os
import sys
import unittest

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
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
        for text in ["Привет! Чем могу помочь?", "Сделал громче", "-v Zarvox привет",
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
            "Душанбе: сейчас плюс восемнадцать градусов Цельсия, ощущается как плюс шестнадцать градусов Цельсия, "
            "ветер три метра в секунду. Сегодня от минус двух градусов Цельсия до плюс двадцати одного градуса Цельсия.")

    def test_agreement(self):
        cases = [("1°C", "один градус Цельсия"), ("2°C", "два градуса Цельсия"), ("11°C", "одиннадцать градусов Цельсия"),
                 ("21°C", "двадцать один градус Цельсия"), ("2,5°C", "две целых пять десятых градуса Цельсия"),
                 ("41%", "сорок один процент"), ("12%", "двенадцать процентов"), ("1 м/с", "один метр в секунду"),
                 ("5 м/с", "пять метров в секунду"), ("0,5 мм", "ноль целых пять десятых миллиметра")]
        for text, expected in cases:
            self.assertEqual(prepare_for_speech(text), expected, text)

    def test_currencies(self):
        self.assertEqual(prepare_for_speech("1 USD = 10,93 TJS"),
                         "один доллар равно десять целых девяносто три сотых сомони")
        self.assertEqual(prepare_for_speech("$5 и 100 ₽, 3 EUR, 2 CNY, 7 сомони"),
                         "пять долларов и сто рублей, три евро, два юаня, семь сомони")

    def test_plain_degrees_and_numbers_unchanged(self):
        for text in ["Версия 3.5", "Время 12:30"]:
            self.assertEqual(prepare_for_speech(text), text)
        self.assertEqual(prepare_for_speech("Сейчас 25° тепла, это № 1 по погоде."),
                         "Сейчас двадцать пять градусов тепла, это номер один по погоде.")
        print("✅ Числа и единицы: OK")


class TestNumbersForSpeech(unittest.TestCase):
    """Модель пишет числа цифрами — словами их делает prepare_for_speech (5.2.1)."""

    def test_ten_examples(self):
        cases = [
            ("Население 1 228 000 человек.", "Население один миллион двести двадцать восемь тысяч человек."),
            ("Высота 793 метра.", "Высота семьсот девяносто три метра."),
            ("Осадков 2,5 мм.", "Осадков две целых пять десятых миллиметра."),
            ("Будет 10–12 градусов.", "Будет от десяти до двенадцати градусов."),
            ("Это было в 2026 году.", "Это было в две тысячи двадцать шестом году."),
            ("Наступил 2026 год, итоги 2025 года.",
             "Наступил две тысячи двадцать шестой год, итоги две тысячи двадцать пятого года."),
            ("Сегодня +16°C, влажность 41%.", "Сегодня плюс шестнадцать градусов Цельсия, влажность сорок один процент."),
            ("Около 5 000 000 000 человек.", "Около пяти миллиардов человек."),
            ("Подождите 1 минуту, прошла 21 секунда.", "Подождите одну минуту, прошла двадцать одна секунда."),
            ("Обновлено 27 октября, а сегодня 28 октября.",
             "Обновлено двадцать седьмого октября, а сегодня двадцать восьмое октября."),
            ("Курс 1 USD = 9,21 TJS.", "Курс один доллар равно девять целых двадцать одна сотая сомони."),
        ]
        for text, expected in cases:
            self.assertEqual(prepare_for_speech(text), expected, text)

    def test_phones_versions_dates_kept_as_digits(self):
        for text in ["Телефон +7 999 123-45-67.", "Звоните 999 123 45 67", "Обновите до iOS 17.2, версия 3.5.",
                     "Дата 27.10.2026, время 12:30, половина 1/2.", "Дата 2026-10-27", "R2-D2, COVID-19, mp3",
                     "открой 2-ю ссылку"]:
            self.assertEqual(prepare_for_speech(text), text)

    def test_number_words(self):
        from core import numbers
        self.assertEqual(numbers.cardinal(0), "ноль")
        self.assertEqual(numbers.cardinal(1011), "одна тысяча одиннадцать")
        self.assertEqual(numbers.cardinal(2_000_001), "два миллиона один")
        self.assertEqual(numbers.cardinal(228, genitive=True), "двухсот двадцати восьми")
        self.assertEqual(numbers.ordinal(2000), "двухтысячный")
        self.assertEqual(numbers.ordinal(2023, "prep"), "две тысячи двадцать третьем")
        self.assertEqual(numbers.ordinal(1990, "gen"), "одна тысяча девятьсот девяностого")
        print("✅ Числа словами: OK")


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
