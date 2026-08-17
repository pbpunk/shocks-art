# Prompts

## Editorial Analysis Prompt v1.0

See `EDITORIAL_PROMPT` in `app/services/gemini.py`.

The prompt explicitly tells Gemini not to choose the first acceptable segment by default. It must scan for multiple plausible windows across the stream, return up to three ranked non-overlapping candidates, and explain why each selected window is distinct from other selected or plausible alternatives.

It also requires transcript and visual evidence to include exact timestamps inside the selected window. If Gemini cannot verify in-window speech, it must leave transcript evidence empty rather than borrowing transcript text from elsewhere in the stream.

## Repair Prompt v1.0

See `REPAIR_PROMPT` in `app/services/gemini.py`.

Repair receives only schema version, validation errors, and previous response. It must not perform a new video analysis.
