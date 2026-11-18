import textkit


def test_shout():
    assert textkit.shout("hi there") == "HI THERE!"


def test_word_count():
    assert textkit.word_count("one two  three") == 3


def test_initials():
    assert textkit.initials("portable network graphics") == "PNG"
