import unittest

from publication_intent import requested, publication_only, destination_matches


class PublicationIntentTests(unittest.TestCase):
    def test_explicit_human_requests(self):
        for text in ('Юки, добавь змейку и сделай push', 'Юки, опубликуй изменения',
                     'Сударь, отправь результат на GitHub', 'push changes please',
                     'Юки, змейка готова, push не был сделан, сделай сейчас',
                     'Можешь сделать push?', 'Юки, push', 'Юки, запушь', 'Можешь опубликовать?',
                     'юки, попробуй сейчас сделать push',
                     'юки опубликуй текущий файл в репозиторий, не смотря на отсутствие изменений'):
            with self.subTest(text=text):
                self.assertTrue(requested(text))

    def test_discussion_quotes_and_prohibitions_are_not_permission(self):
        for text in ('Добавь калькулятор', 'Не делай push', 'Как сделать push?',
                     'Юки, цитата: сделай push', 'Юки, "сделай push"',
                     'Добавь змейку без публикации', 'Сделай только commit',
                     'Почему ты не сделал push?', 'Do not push changes',
                     'Не запушь случайно', 'Не опубликуй изменения', 'Я уже запушил изменения'):
            with self.subTest(text=text):
                self.assertFalse(requested(text))

    def test_mixed_work_is_not_publication_only(self):
        self.assertFalse(publication_only('Юки, добавь змейку и сделай push'))
        self.assertFalse(publication_only('Юки, сделай калькулятор и push'))
        self.assertFalse(publication_only('Юки, сделай улучшение и push'))
        self.assertTrue(publication_only('Юки, сделай push'))
        self.assertTrue(publication_only('Юки, опубликуй исправления как есть'))
        self.assertTrue(publication_only('Юки, попробуй сейчас сделать push'))

    def test_explicit_destination_must_match(self):
        remote = 'https://github.com/example/project.git'
        self.assertTrue(destination_matches('Сделай push в ветку main', 'main', remote))
        self.assertFalse(destination_matches('Сделай push в ветку other', 'main', remote))
        self.assertFalse(destination_matches('Опубликуй https://github.com/other/project', 'main', remote))
