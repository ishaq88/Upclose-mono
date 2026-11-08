"""Report briefing: buyer advisory block, paid narration audio, and spoken capture guide.

Provider calls stay behind the existing advisor and voice adapters. Everything here
fails closed: a provider problem yields a `not_available` block or an HTTP problem,
never a clean-looking result.
"""
