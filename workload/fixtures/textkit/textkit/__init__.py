"""Small string helpers."""


def shout(text):
    return text.upper() + "!"


def word_count(text):
    return len(text.split())


def initials(text):
    return "".join(word[0].upper() for word in text.split())
