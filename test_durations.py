"""Парсер длительностей: цифры и числительные, «полчаса», «полтора часа», составные."""
import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import tests_base  # noqa: F401 — защита от настоящих запусков и записи в домашние папки
from core.durations import duration_hint, duration_phrase, parse_duration


class TestParseDuration(unittest.TestCase):
    def test_task_examples(self):
        cases = {"5 минут": 300, "пять минут": 300, "полчаса": 1800, "час": 3600, "полтора часа": 5400,
                 "1 час 30 минут": 5400, "90 секунд": 90}
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), seconds)

    def test_words_and_forms(self):
        cases = {"две минуты": 120, "двадцать пять минут": 1500, "сто двадцать минут": 7200, "одиннадцать минут": 660,
                 "сорок пять секунд": 45, "минуту": 60, "секунду": 1, "полторы минуты": 90, "два часа": 7200,
                 "час и тридцать минут": 5400, "1 час и 30 минут": 5400, "три часа": 10800, "10 секунд": 10}
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), seconds)

    def test_not_a_duration(self):
        for text in ["", "тридцать", "5", "пять минут чай", "двадцать пятнадцать минут", "0 минут", "100000 секунд",
                     "25 часов", "полтора", "полтора дня", "привет", "завтра"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_duration(text))

    def test_limits(self):
        self.assertEqual(parse_duration("24 часа"), 86400)
        self.assertEqual(parse_duration("1 секунду"), 1)
        self.assertIsNone(parse_duration("86401 секунду"))

    def test_duration_phrase(self):
        self.assertEqual(duration_phrase(90), "1 минута 30 секунд")
        self.assertEqual(duration_phrase(300), "5 минут")
        self.assertEqual(duration_phrase(5400), "1 час 30 минут")
        self.assertEqual(duration_phrase(3), "3 секунды")
        self.assertEqual(duration_phrase(11 * 60), "11 минут")

    def test_hint_for_model(self):
        self.assertEqual(duration_hint("через 25 минут напомни позвонить"), ("25 минут = 1500 секунд", 1500))
        self.assertEqual(duration_hint("поставь таймер на полтора часа с названием чай")[1], 5400)
        self.assertEqual(duration_hint("через пять минут выключи звук")[1], 300)
        self.assertIsNone(duration_hint("привет, как дела"))
        self.assertIsNone(duration_hint("через неделю"))


if __name__ == "__main__":
    unittest.main()
