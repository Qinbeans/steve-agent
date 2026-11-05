//! Server-sent events parser. Network chunks can split a line or an event anywhere.

#[derive(Default)]
pub struct SseParser {
    buffer: Vec<u8>,
    data: Vec<String>,
}

impl SseParser {
    /// Feed bytes and get the `data` payload of each event they complete.
    pub fn feed(&mut self, bytes: &[u8]) -> Vec<String> {
        self.buffer.extend_from_slice(bytes);
        let mut events = Vec::new();
        while let Some(newline) = self.buffer.iter().position(|&b| b == b'\n') {
            let mut line: Vec<u8> = self.buffer.drain(..=newline).collect();
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            if line.is_empty() {
                if !self.data.is_empty() {
                    events.push(self.data.join("\n"));
                    self.data.clear();
                }
            } else if let Some(value) = line.strip_prefix(b"data:") {
                let value = value.strip_prefix(b" ").unwrap_or(value);
                self.data.push(String::from_utf8_lossy(value).into_owned());
            }
            // `event:` lines repeat the JSON's `type`, and `:` lines are heartbeats.
        }
        events
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_events_and_ignores_comments_and_event_lines() {
        let mut parser = SseParser::default();
        let stream = b"event: a\ndata: {\"n\":1}\n\n: ping\n\nevent: b\ndata: {\"n\":2}\n\n";
        assert_eq!(parser.feed(stream), [r#"{"n":1}"#, r#"{"n":2}"#]);
    }

    #[test]
    fn survives_being_split_at_every_byte() {
        let stream = "data: {\"text\":\"h\u{e9}llo\"}\r\n\r\ndata: 2\n\n".as_bytes();
        let mut parser = SseParser::default();
        let events: Vec<String> = stream.iter().flat_map(|b| parser.feed(&[*b])).collect();
        assert_eq!(events, ["{\"text\":\"h\u{e9}llo\"}", "2"]);
    }

    #[test]
    fn joins_multi_line_data_and_holds_incomplete_events() {
        let mut parser = SseParser::default();
        assert!(parser.feed(b"data: one\ndata: two\n").is_empty());
        assert_eq!(parser.feed(b"\n"), ["one\ntwo"]);
    }
}
