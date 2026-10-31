from upstream.extract import make_ticker, terms_for_post


def test_phrases_hashtags_and_words():
    t = terms_for_post("Deleted every app. Analog weekend, highly recommend! #AnalogWeekend",
                       tags="analogweekend offline")
    assert t["#analogweekend"] == "hashtag"
    assert t["#offline"] == "hashtag"
    assert t["analog weekend"] == "phrase"
    assert t["highly recommend"] == "phrase"
    assert t["deleted"] == "word"


def test_stopword_edges_are_dropped():
    t = terms_for_post("this is the analog weekend of the year")
    assert "analog weekend" in t
    assert "the analog" not in t
    assert "weekend of" not in t
    assert "is the" not in t


def test_slang_with_common_words_survives():
    assert "no notes" in terms_for_post("no notes honestly")
    assert "it's giving" in terms_for_post("it's giving cardio")
    assert "i would" not in terms_for_post("i would never")  # all stopwords


def test_urls_mentions_and_numbers_ignored():
    t = terms_for_post("@someone.bsky.social check https://example.com/x 2026 analog weekend")
    assert not any("example" in k or "bsky" in k or "someone" in k for k in t)
    assert not any(k.isdigit() for k in t)
    assert "analog weekend" in t


def test_phrases_do_not_cross_punctuation():
    t = terms_for_post("trying an analog weekend, no phone. Back Monday")
    assert "analog weekend" in t
    assert "no phone" in t
    assert "weekend no" not in t
    assert "analog weekend no" not in t
    assert "phone back" not in t


def test_repeated_tokens_not_phrases():
    t = terms_for_post("lol lol lol so so good")
    assert "lol lol" not in t


def test_counted_once_per_post():
    t = terms_for_post("analog weekend analog weekend analog weekend")
    assert list(t).count("analog weekend") == 1


def test_words_can_be_disabled():
    t = terms_for_post("analog weekend forever", include_words=False)
    assert "forever" not in t
    assert "analog weekend" in t


def test_curly_apostrophes_normalised():
    t = terms_for_post("it’s giving cardio")
    assert "giving cardio" in t


def test_ticker():
    assert make_ticker("analog weekend") == "ANLGWKND"
    assert make_ticker("gas station chic") == "GSSTCH"
    assert len(make_ticker("an extremely long phrase with many words")) <= 8
    assert make_ticker("analog weekend", {"ANLGWKND"}) == "ANLGWKN2"
