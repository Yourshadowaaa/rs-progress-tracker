from .rules import PARSER_VERSION, RULES, ParsedEvent

__all__ = ["PARSER_VERSION", "ParsedEvent", "parse_entry", "match_rule"]


def match_rule(text: str, details: str) -> tuple[str, ParsedEvent]:
    """Return (rule_name, event). Unmatched entries become an `unparsed` event."""
    fields = {"text": text.strip(), "details": details.strip()}
    for rule in RULES:
        m = rule.pattern.search(fields[rule.field])
        if m:
            event = rule.build(m, fields["text"], fields["details"])
            if event is not None:
                return rule.name, event
    return "unparsed", ParsedEvent("unparsed", fields["text"][:300] or fields["details"][:300], known_subject=False)


def parse_entry(text: str, details: str) -> list[ParsedEvent]:
    """Deterministic: the same (text, details) always yields the same events."""
    return [match_rule(text, details)[1]]
