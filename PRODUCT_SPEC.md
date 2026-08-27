# Product Spec

## Mission

Convert long Shocks Art livestream archives into categorized, searchable, traceable source footage for short-form editing while preserving human editorial control.

## MVP Users

- Nate or an editor reviewing candidate source segments.
- A producer exporting approved metadata to Vizard or manual editing.

## MVP Workflow

1. Configure the YouTube channel handle and API keys.
2. Discover public archived livestreams through the YouTube Data API.
3. Store each livestream as a root `Stream`.
4. Analyze one selected stream or the queued archive with Gemini.
5. Validate Gemini JSON against the versioned schema.
6. Save ranked `CandidateWindow` rows per stream.
7. Review, approve, reject, mark later, adjust timestamps, edit pillar and tags.
8. View ranked top-five candidates.
9. Export CSV or JSON for downstream editing.

## Long-Term Product North Star: Conversational Trace Retrieval

The durable product direction is a chatbot/retrieval layer over grounded, time-aligned traces rather than generic chat over video summaries or filenames.

Target user language should include requests such as:

> "A clip of me sanding the axes I worked on last week."

The system should resolve natural-language references across identity, action, object/project continuity, and real-world time, then return a usable source moment or clip with exact provenance.

The retrieval architecture should preserve `Media -> Traces -> Associations -> Entities`:

- **Traces** are the primary evidentiary substrate: time-aligned observations such as speech, actions, objects, visual states, OCR, audio events, and scene context.
- **Associations** connect traces across time and interpretation, such as `same_object_as`, `part_of_project`, `material_used_on`, `continuation_of`, or `person_working_on_object`.
- **Entities** provide persistent identity for people, individual pieces/projects, tools, materials, places, and other recurring subjects.
- **Retrieval/chat** should use traces as the source of truth and Associations/Entities to broaden, constrain, and resolve references.

High-value query classes include:

- identity + action + time: "me sanding the axes I worked on last week"
- persistent-object retrieval: "show me every time this staff appears"
- project chronology: "when did I first start that mushroom lamp?"
- visual-only retrieval: "B-roll of torching wood"
- speech + visual overlap: "find me explaining something while actually demonstrating it"
- state/change retrieval: "when did this crack first appear?"
- cross-session memory: "what did I say about this piece before I finished it?"

Trace records therefore need reliable source and real-world timing (`media_id`, media-relative `start/end`, and trustworthy `captured_at`), modality, observed content, provenance/confidence, embeddings, and links to people/objects/projects/actions. Session identity and persistent entity identity are necessary for queries spanning multiple videos or days.

A core grounding rule should be preserved as the system evolves: **no derived answer or `CandidateWindow` should claim factual evidence that cannot resolve back to supporting traces in the relevant source interval.** Captions are the first speech-trace implementation; future multimodal traces should generalize the same contract.

## Exclusions

The MVP does not include automatic publishing, performance optimization, autonomous strategy, recursive agent loops, or full minute-by-minute indexing.
