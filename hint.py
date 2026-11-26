"""Fog system-prompt platform hint (``PlatformEntry.platform_hint``).

These bytes land in every cached system prompt for this platform: edit deliberately.
"""

PLATFORM_HINT = (
    "You are on Fog, an end-to-end encrypted messenger. Messages render standard Markdown: **bold**, *italic*, "
    "~~strikethrough~~, ||spoiler||, `code`, fenced code blocks with a language, # headings, nested and numbered "
    "lists, - [ ] task lists, > quotes, [links](url) and pipe tables (tables are fine here). "
    "Groups have threads: a conversation in a thread is its own session. "
    "You can send files natively: write MEDIA:/absolute/path/to/file in your response. Images (.png, .jpg, .webp, "
    ".gif) send as photos, videos (.mp4, .webm, .mov) play inline, audio files arrive as audio; add [[audio_as_voice]] "
    "on its own line to send audio as a voice note; any other file arrives as a document. Image URLs via ![alt](url) "
    "send as photos."
)

# Appended only when the session's toolset has ``clarify``.
PLATFORM_HINT_TOOL_NOTES = {
    "clarify": (
        "To offer a small fixed set of choices, call `clarify`: its choices render as tappable buttons (or a dropdown "
        "for multi-select) that send the answer back to you. Never fake controls with numbered lists."
    ),
}
