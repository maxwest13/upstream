//! Turning Jetstream events into rows. Pure functions, unit-tested below.

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const MAX_TEXT_CHARS: usize = 1000;

#[derive(Debug, Deserialize)]
pub struct Event {
    pub did: String,
    pub time_us: u64,
    pub kind: String,
    #[serde(default)]
    pub commit: Option<Commit>,
}

#[derive(Debug, Deserialize)]
pub struct Commit {
    pub operation: String,
    pub collection: String,
    pub rkey: String,
    #[serde(default)]
    pub record: Option<Record>,
}

#[derive(Debug, Deserialize)]
pub struct Record {
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub langs: Vec<String>,
    #[serde(default)]
    pub facets: Vec<Facet>,
}

#[derive(Debug, Deserialize)]
pub struct Facet {
    #[serde(default)]
    pub features: Vec<serde_json::Value>,
}

/// One row for raw_posts.
#[derive(Debug, Clone, PartialEq)]
pub struct Post {
    pub time: SystemTime,
    pub time_us: u64,
    pub uri: String,
    pub author_hash: String,
    pub lang: String,
    pub text: String,
    pub tags: String,
}

/// Returns a Post for a newly created English (or other wanted-language) post
/// with non-empty text; None for everything else (likes, deletes, replies
/// without text, other languages).
pub fn to_post(ev: &Event, langs: &[String], salt: &str) -> Option<Post> {
    if ev.kind != "commit" {
        return None;
    }
    let commit = ev.commit.as_ref()?;
    if commit.operation != "create" || commit.collection != "app.bsky.feed.post" {
        return None;
    }
    let rec = commit.record.as_ref()?;
    let text = rec.text.trim();
    if text.is_empty() {
        return None;
    }
    let lang = if langs.is_empty() {
        rec.langs.first().cloned().unwrap_or_default()
    } else {
        rec.langs
            .iter()
            .find(|l| langs.iter().any(|w| l.to_lowercase().starts_with(&w.to_lowercase())))?
            .clone()
    };

    let mut tags: Vec<String> = rec
        .facets
        .iter()
        .flat_map(|f| f.features.iter())
        .filter(|feat| feat.get("$type").and_then(|t| t.as_str()) == Some("app.bsky.richtext.facet#tag"))
        .filter_map(|feat| feat.get("tag").and_then(|t| t.as_str()))
        .map(|t| t.trim_start_matches('#').to_lowercase())
        .filter(|t| !t.is_empty() && !t.contains(char::is_whitespace))
        .collect();
    tags.sort();
    tags.dedup();

    Some(Post {
        time: UNIX_EPOCH + Duration::from_micros(ev.time_us),
        time_us: ev.time_us,
        uri: format!("at://{}/app.bsky.feed.post/{}", ev.did, commit.rkey),
        author_hash: hash_author(&ev.did, salt),
        lang,
        text: text.chars().take(MAX_TEXT_CHARS).collect(),
        tags: tags.join(" "),
    })
}

/// Salted, truncated SHA-256 of the author id. Lets us count distinct authors
/// without storing who they are.
pub fn hash_author(did: &str, salt: &str) -> String {
    let mut h = Sha256::new();
    h.update(salt.as_bytes());
    h.update(b":");
    h.update(did.as_bytes());
    hex::encode(&h.finalize()[..8])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(json: &str) -> Event {
        serde_json::from_str(json).unwrap()
    }

    const POST: &str = r#"{
        "did":"did:plc:abc123","time_us":1793360000000000,"kind":"commit",
        "commit":{"rev":"x","operation":"create","collection":"app.bsky.feed.post","rkey":"3kxyz",
          "record":{"$type":"app.bsky.feed.post","createdAt":"2026-10-30T12:00:00Z",
            "langs":["en"],"text":"  Deleted every app for the weekend. #AnalogWeekend #offline  ",
            "facets":[
              {"index":{"byteStart":0,"byteEnd":5},"features":[{"$type":"app.bsky.richtext.facet#tag","tag":"AnalogWeekend"}]},
              {"index":{"byteStart":6,"byteEnd":9},"features":[{"$type":"app.bsky.richtext.facet#tag","tag":"offline"}]},
              {"index":{"byteStart":6,"byteEnd":9},"features":[{"$type":"app.bsky.richtext.facet#link","uri":"https://x.y"}]}
            ]},
          "cid":"bafy"}}"#;

    fn en() -> Vec<String> {
        vec!["en".into()]
    }

    #[test]
    fn parses_english_post_with_tags() {
        let p = to_post(&ev(POST), &en(), "salt").unwrap();
        assert_eq!(p.uri, "at://did:plc:abc123/app.bsky.feed.post/3kxyz");
        assert_eq!(p.text, "Deleted every app for the weekend. #AnalogWeekend #offline");
        assert_eq!(p.tags, "analogweekend offline");
        assert_eq!(p.lang, "en");
        assert_eq!(p.author_hash.len(), 16);
        assert_eq!(p.time, UNIX_EPOCH + Duration::from_micros(1793360000000000));
    }

    #[test]
    fn skips_other_languages() {
        let j = POST.replace(r#""langs":["en"]"#, r#""langs":["ja"]"#);
        assert!(to_post(&ev(&j), &en(), "salt").is_none());
    }

    #[test]
    fn accepts_regional_language_codes() {
        let j = POST.replace(r#""langs":["en"]"#, r#""langs":["en-US"]"#);
        assert!(to_post(&ev(&j), &en(), "salt").is_some());
    }

    #[test]
    fn skips_deletes_and_identity_events() {
        let del = r#"{"did":"did:plc:a","time_us":1,"kind":"commit",
            "commit":{"rev":"x","operation":"delete","collection":"app.bsky.feed.post","rkey":"k"}}"#;
        assert!(to_post(&ev(del), &en(), "s").is_none());
        let ident = r#"{"did":"did:plc:a","time_us":1,"kind":"identity","identity":{"did":"did:plc:a"}}"#;
        assert!(to_post(&ev(ident), &en(), "s").is_none());
    }

    #[test]
    fn skips_likes_and_empty_text() {
        let like = POST.replace("app.bsky.feed.post\",\"rkey", "app.bsky.feed.like\",\"rkey");
        assert!(to_post(&ev(&like), &en(), "s").is_none());
        let empty = POST.replace(
            "  Deleted every app for the weekend. #AnalogWeekend #offline  ",
            "   ",
        );
        assert!(to_post(&ev(&empty), &en(), "s").is_none());
    }

    #[test]
    fn author_hash_is_stable_and_salted() {
        assert_eq!(hash_author("did:plc:a", "s1"), hash_author("did:plc:a", "s1"));
        assert_ne!(hash_author("did:plc:a", "s1"), hash_author("did:plc:a", "s2"));
        assert_ne!(hash_author("did:plc:a", "s1"), hash_author("did:plc:b", "s1"));
    }

    #[test]
    fn truncates_long_text() {
        let long = "a".repeat(5000);
        let j = POST.replace(
            "  Deleted every app for the weekend. #AnalogWeekend #offline  ",
            &long,
        );
        let p = to_post(&ev(&j), &en(), "s").unwrap();
        assert_eq!(p.text.chars().count(), MAX_TEXT_CHARS);
    }
}
