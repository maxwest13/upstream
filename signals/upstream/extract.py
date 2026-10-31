"""Turn post text into candidate terms: hashtags, phrases and (optionally) words.

Each post contributes each term at most once, so one excited author repeating
a phrase five times in a post counts once.
"""
import re

STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren't as at be because been before being below
between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each
even ever every few for from further get gets getting go goes going gonna got had hadn't has hasn't have
haven't having he he'd he'll he's her here here's hers herself him himself his how how's i i'd i'll i'm i've
if in into is isn't it it's its itself just let's like lol me more most much must mustn't my myself new no nor
not now of off oh ok okay on once one only or other ought our ours ourselves out over own really right
same say said says see she she'd she'll she's should shouldn't so some still such than that that's the
their theirs them themselves then there there's these they they'd they'll they're they've thing things think
this those though through time to too two u up us very via want was wasn't way we we'd we'll we're we've
well were weren't what what's when when's where where's which while who who's whom why why's will with won't
would wouldn't yeah yes yet you you'd you'll you're you've your yours yourself yourselves amp rt im dont
cant didnt doesnt isnt wasnt youre thats theres ive id ill
""".split())

# Narrower list used only at phrase edges. Slang often starts with words like
# "no", "it's", "giving" or "main" ("no notes", "it's giving"), so only pure
# function words disqualify an edge.
EDGE_STOP = frozenset("""
a an the and or but nor of to in on at for with by from as is are was were be been being am this that these
those it its if then than so very just really also into about over after before via amp rt
""".split())

URL_RE =re.compile(r"https?://\S+|www\.\S+|\b\S+\.(?:com|org|net|io|app|social|co|ly)\S*", re.I)
MENTION_RE = re.compile(r"@[\w.\-]+")
HASHTAG_RE = re.compile(r"#(\w{2,64})", re.UNICODE)
TOKEN_RE = re.compile(r"[^\W\d_](?:[\w'’]*[^\W_])?", re.UNICODE)


CLAUSE_RE = re.compile(r"[.,!?;:()\[\]{}\"“”…|/\n\r\t–—-]+")


def _clauses(text: str) -> list[list[str]]:
    """Token lists per clause, so phrases never span punctuation."""
    text = URL_RE.sub(" . ", text)
    text = MENTION_RE.sub(" . ", text)
    text = HASHTAG_RE.sub(" . ", text)
    return [
        [t.replace("’", "'").lower() for t in TOKEN_RE.findall(part)]
        for part in CLAUSE_RE.split(text)
    ]


def terms_for_post(text: str, tags: str = "", include_words: bool = True) -> dict[str, str]:
    """Return {term: kind} for one post. kind is 'hashtag', 'phrase' or 'word'."""
    out: dict[str, str] = {}

    tag_set = {t.lower() for t in tags.split() if t}
    tag_set |= {m.lower() for m in HASHTAG_RE.findall(text)}
    for t in tag_set:
        if not t.isdigit():
            out["#" + t] = "hashtag"

    for toks in _clauses(text):
        for n in (2, 3):
            for i in range(len(toks) - n + 1):
                gram = toks[i:i + n]
                if gram[0] in EDGE_STOP or gram[-1] in EDGE_STOP:
                    continue
                if all(g in STOPWORDS for g in gram):
                    continue
                if any(len(g) < 2 for g in gram):
                    continue
                if len(set(gram)) < n:  # "lol lol", "no no no"
                    continue
                out.setdefault(" ".join(gram), "phrase")

        if include_words:
            for t in toks:
                if len(t) >= 4 and t not in STOPWORDS:
                    out.setdefault(t, "word")

    return out


_VOWELS = set("aeiou")


def make_ticker(term: str, taken: set[str] | None = None, max_len: int = 8) -> str:
    """'analog weekend' -> 'ANLGWKND'. First letter of each word plus its
    consonants, uppercased, made unique against `taken` with a numeric suffix."""
    taken = taken or set()
    words = re.findall(r"[a-z0-9]+", term.lower())
    if not words:
        words = ["x"]
    parts = [w[0] + "".join(c for c in w[1:] if c not in _VOWELS) for w in words]
    base = "".join(parts).upper()
    if len(base) > max_len:
        per = max(1, max_len // len(parts))
        base = "".join(p[:per] for p in parts).upper()[:max_len]
    base = base or "X"
    ticker, n = base, 2
    while ticker in taken:
        suffix = str(n)
        ticker = base[: max_len - len(suffix)] + suffix
        n += 1
    return ticker
