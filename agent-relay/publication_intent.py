"""Publication permission comes from the current human text, never chat history."""
import re


def requested(text: str) -> bool:
    text = text.casefold()
    # Quoted examples, questions and explicit prohibitions are not permission.
    if any(mark in text for mark in ('```', 'цитат', 'например')):
        return False
    text = re.sub(r'"[^"\n]*"|«[^»\n]*»', '', text)
    question = re.search(r'\b(?:как|почему|зачем|что такое|можно ли|how|why|what is)\b', text)
    command = re.search(r'\b(?:сделай|попробуй|прошу|можешь|можете|опубликуй|отправь|залей|выложи|запушь|запуши|push|publish)\b', text)
    if question and (not command or question.start() < command.start()):
        return False
    if re.search(r'\b(?:не\s+(?:делай|делать|нужно|надо|публик\w*|опублик\w*|отправ\w*|запуш\w*|пуш\w*|коммит\w*|коммить)|без\s+(?:push|публикации|коммита)|do not|don.t)\b', text):
        return False
    return bool(re.search(
        r'\b(?:запушь(?:те)?|запуши(?:те|м)?|опубликуй(?:те)?|публикуй|пушни|пушь)\b|'
        r'\b(?:можешь|можете|нужно|надо|прошу)\b[^\n.!?]{0,60}\b(?:запушить|опубликовать)\b|'
        r'\b(?:сделай|сделать|выполни|выполнить|делай|нужно|надо)\b[^\n.!?]{0,100}\b(?:push|пуш)\b|'
        r'\b(?:отправь|залей|выложи|опубликовать)\b[^\n.!?]{0,100}\b(?:github|гитхаб|репозиторий)\b|'
        r'\b(?:push|publish)\b[^\n.!?]{0,80}\b(?:please|changes|github|сделай|выполни)\b|'
        r'\b(?:and|then|please|can you)\s+(?:push|publish)\b|'
        r'^(?:[^,\n]{0,30},\s*)?(?:please\s+)?(?:push|publish)\s*[.!]?$', text))


def destination_matches(text: str, branch: str, remote: str) -> bool:
    branches = re.findall(r'\b(?:ветк[ауеи]|branch)\s+[`\']?([\w./-]+)', text, re.I)
    urls = re.findall(r'https://github\.com/[\w.-]+/[\w.-]+', text, re.I)
    return (all(value == branch for value in branches)
            and all(value.removesuffix('.git').casefold() == remote.removesuffix('.git').casefold() for value in urls))


def publication_only(text: str) -> bool:
    """Keep mixed development + publication requests in the task pipeline."""
    text = text.casefold()
    editing = re.search(
        r'\b(?:добавь|добавить|исправь|исправить|улучши|улучшить|реализуй|реализовать|создай|создать|напиши|написать|разработай|разработать|implement|fix|add|build|create)\b|'
        r'\b(?:сделай|сделать)\s+(?!(?:commit|push|пуш|коммит|это|сейчас)\b)\w+', text)
    return requested(text) and not editing
