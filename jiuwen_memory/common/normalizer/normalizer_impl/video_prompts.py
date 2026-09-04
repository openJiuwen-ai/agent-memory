# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# ruff: noqa: E501

CHAPTER_CHUNK = """
You are given a time-aligned transcript of a video.
Each line contains a timestamp and the corresponding spoken content.

Your task is to segment the transcript into coherent CHAPTERS based on semantic, structural, or intent transitions.
Do NOT divide chapters into arbitrarily small segments.

---------------------------------------
Core Segmentation Principles
---------------------------------------

A chapter should represent a coherent and self-contained unit of discourse.

Chapters may be formed based on:

1) Structural Unit Shift (HIGHEST PRIORITY)
   - Explicit section/unit markers:
     numbered items, steps, parts, episodes, cases,
     years/phases, labeled segments.
   - Clear transition phrases:
     "next", "now let's", "moving on", "part two", etc.
   - When a new named unit becomes dominant,
     this strongly indicates a boundary.

2) Semantic Topic Shift
   - Clear change in subject, goal, or organizing focus.
   - A new discussion thread or objective becomes primary.
---------------------------------------
Granularity Control
---------------------------------------

- Do NOT segment by fixed time intervals.
- Do NOT over-segment minor tone or sentence changes.
- Prefer meaningful structural or semantic transitions.
- In clearly structured formats, align chapters with major units.

---------------------------------------
Low-Information Handling
---------------------------------------

If semantic signal is weak:

- Still attempt segmentation based on any identifiable transitions.
- Always output the inferred chapters.
- Use segmentation_confidence to reflect boundary reliability,
  NOT to suppress segmentation.

---------------------------------------
Coverage Rules
---------------------------------------

- Chapters must fully cover the entire timeline.
- No gaps or overlaps.
- Timestamp gaps belong to the previous chapter.
- Chapters must remain minimally coherent.

---------------------------------------
Required Output Fields
---------------------------------------

Each chapter must include:
- chapter_id
- start_time
- title (<= 12 words)
- summary (1 concise sentence)

---------------------------------------
Segmentation Confidence (REDEFINED)
---------------------------------------

segmentation_confidence reflects the RELIABILITY of ASR-based boundary evidence,
especially whether boundaries are structurally explicit and precisely alignable.

Set as:

Assign ASR segmentation confidence using only two levels: HIGH or LOW.

HIGH:
- ASR provides reliable evidence for semantic progression, topic shift, task shift, or stage transition,
- and it is meaningfully useful for chapter segmentation,
- even if the structure is not explicitly labeled with phrases like "part 1" or "next section".

LOW:
- ASR is not a reliable basis for chapter segmentation.
- This includes cases where the audio is dominated by:
  - lyrics, singing, repeated chorus-like language,
  - pantomime / near-silent performance,
  - music or environmental sound with little usable speech,
  - extremely sparse, noisy, or badly recognized speech,
  - casual fragmented dialogue that does not provide stable structural cues.
- In these cases, chapter boundaries would be mostly subjective or visually inferred, and ASR offers little reliable anchoring.

Important:
Use LOW only when ASR is genuinely weak or uninformative for segmentation,
not merely because explicit section markers are absent.

Important:
LOW means ASR boundary evidence is weak,
NOT that segmentation should be avoided.

---------------------------------------
Output Requirements
---------------------------------------

- Output MUST be valid JSON.
- Output ONLY the JSON object.
- No explanations or extra text.

JSON schema:

{
  "segmentation_confidence": "high|low",
  "chapters": [
    {
      "chapter_id": number,
      "start_time": "HH:MM:SS",
      "title": string,
      "summary": string
    }
  ]
}
"""

CAPTION_PROMPT = """
# Role
You convert the CURRENT video segment(clip) into a precise visual memory unit.

# Event Table (ET)
ET records the ongoing event context from earlier clips.
Use ET only as a weak continuity cue for the CURRENT clip.

You may use ET to:
- keep naming of visibly recurring people / objects / places consistent
- recognize when the same visible event or setup continues
- maintain light local continuity in description

If the clip clearly continues the same visible event, you may use light continuity phrases.

Rules:
- ET is NOT ground truth and may be outdated.
- NEVER describe something only because ET mentions it.
- NEVER copy ET text.
- If ET conflicts with the current visuals, trust the current clip.
- If something is not visually present now, do not include it just for continuity.
- If ET is empty, ignore it.

{event_table}

# Task
Describe the CURRENT segment as completely and accurately as possible.

Focus on:
- people, objects, animals
- actions and interactions
- scene layout and environment
- on-screen text or graphics
- visible temporal progression

# Priority
1. The CURRENT clip is the source of truth.
2. This is NOT an event summary.
3. Do not omit visible details for the sake of continuity.

# Requirements
- Capture the main visible content faithfully.
- Preserve important concrete details that may matter for later QA or retrieval.
- Prefer temporally ordered description when actions unfold over time.
- Be factual and visually grounded.
- Prefer concrete visual descriptors (clothing, color, objects, gestures).
- Do not infer unseen events.

# Output JSON
{
  "detailed_caption": "Temporally ordered visual description grounded in the current clip.",
  "visual_summary": "Short subject-verb-object phrase for indexing.",
  "environment": "Scene description."
}

# Constraints
- Output JSON only.
- Do not add new keys.
- visual_summary ≤ 15 words.
"""


SESSION_SUMMARY_PROMPT_TEMPLATE = """
# Role
You are an event memory consolidation and reasoning assistant.
Your task is to merge multiple short video segment descriptions into ONE coherent EVENT-level memory.

# Input
The following atomic segment descriptions are ordered by time:
{detail_list}

Each segment may contain:
- visual observations of actions or scene changes
- ASR transcripts capturing spoken dialogue

ASR transcripts may contain recognition errors, but they can provide useful clues about goals, relationships, decisions, or conflicts that are not fully visible.

# Objective
Construct an event memory with TWO layers:

1) Event Narrative (what happened)
Reconstruct the episode as a coherent sequence with clear temporal and causal structure by integrating both visual actions and spoken dialogue when relevant.

2) Semantic Inference (what higher-level knowledge can be inferred from the event)
Provide a compact semantic memory that captures the event’s likely goal, relationship clues, roles, habits, or deeper task structure based on the full sequence.

# Instructions

## 1. Topic Label
Provide a concise label capturing the core episode or objective.
Avoid generic labels like "people talking" or "walking".

## 2. Event Narrative
Merge all segments into a single coherent narrative.

The narrative should clearly describe:
- Initial context or situation
- Trigger or turning point
- Key actions, interactions, or attempts
- Important spoken exchanges when they affect the meaning of the event
- Final consequence or state change

Preserve chronological order but organize actions into meaningful phases rather than listing clips.

Use causal or temporal connectors when appropriate.

Preserve important factual details useful for QA, including:
entities, objects, locations, numbers, significant visual changes, and relevant spoken information.

## 3. Semantic Inference
Based on the entire event, provide a compact high-level semantic memory.

Focus on extracting useful higher-level knowledge beyond the surface narrative, such as:
- the apparent goal or objective of the event
- possible relationship dynamics between key entities
- roles, habits, preferences, or capabilities suggested by repeated behavior
- whether repeated actions are serving a larger task, rescue, conflict, plan, or decision process

Use dialogue as supporting evidence when it helps reveal goals, relationships, decisions, or conflicts that are not fully visible.

IMPORTANT:
- Do not simply restate the narrative.
- Prefer useful semantic abstraction over general commentary.
- Avoid vague statements about symbolism, creativity, atmosphere, or moral meaning.
- Separate direct evidence from interpretation.
- Use cautious wording when uncertain.
- Do NOT convert weak inference into confirmed facts.

## 4. Evidence Discipline
- Only infer motivations or causal explanations supported by the provided descriptions.
- Treat ASR transcripts cautiously if they appear noisy, incomplete, or isolated.
- If evidence is incomplete, preserve uncertainty rather than forcing conclusions.
- Do NOT hallucinate missing events or identities.

# Output Format
Return ONLY the following JSON:

{
  "topic_label": "...",
  "full_narrative": "...",
  "semantic_inference": "..."
}

# Constraints
1) Do not output a list of clips; produce one unified event memory.
2) Focus on causal structure and state transitions rather than exhaustive low-level detail.
3) Semantic inference must be higher-level than the narrative and should add new useful knowledge.
4) Output JSON only. No explanations or markdown.
"""

TIME_SUMMARY_PROMPT = """
# Role
You are a neutral video summarizer responsible for consolidating multiple atomic video segments
that fall within the SAME FIXED TIME WINDOW into a coherent temporal summary.

# Input Context
Below is a list of atomic segment descriptions ordered by time.
All segments come from a continuous, fixed-length time window of the video
(not necessarily aligned with a single real-world event):
{detail_list}

# Instructions
1. Window Labeling:
  - Provide a short descriptive label summarizing what is MOSTLY shown in this time window.
  - Do NOT assume the window corresponds to a complete task or a well-defined event.
  - Prefer descriptive phrases over intentional or goal-oriented wording.

2. Temporal Consolidation:
  - Merge the segments into a fluent, time-ordered narrative.
  - Remove obvious redundancy while preserving chronological progression.
  - Describe what happens, changes, or appears over time within this window.
  - Retain important observable details such as objects, people, actions, locations,
    quantities, colors, on-screen text, and notable interactions.

# Output Format
Return ONLY the following JSON object:

{{
  "topic_label": "Descriptive label for this time window",
  "full_narrative": "A coherent, time-ordered summary of what occurs within this fixed window."
}}
"""


EVENT_RELATIONSHIP = """
You are an event relation classifier for long-form narrative videos.

You will be given TWO events (Event A happens before Event B).
Each event is a summarized description produced by an upstream pipeline.

Your task is to determine whether Event B is narratively connected to Event A,
and if so, how.

The event graph should reflect STORY CONTINUITY within the video itself,
not generic similarity or real-world common sense.

It is better to produce a sparse but meaningful graph
than a dense graph with weak or generic edges.

--------------------------------
RELATION DEFINITIONS
--------------------------------
- "causes":
  Event A directly leads to Event B.
  This requires explicit or clearly described causal linkage.

- "enables":
  Event A establishes a concrete prerequisite for Event B
  (e.g., a decision, an action, or new information that allows B to happen).

- "elaborates":
  Event B continues the SAME specific storyline or subplot as Event A.
  This must be traceable as a continuation of the same situation,
  not merely a similar type of activity.

- "contrasts":
  Event B represents a clear narrative shift away from Event A
  (e.g., escalation vs resolution, cooperation vs conflict).

- "unrelated":
  No narratively traceable connection.

--------------------------------
STORY ANCHOR RULE (CRITICAL)
--------------------------------
You may output "causes", "enables", or "elaborates"
ONLY IF there is a SHARED STORY ANCHOR.

A valid story anchor is:
- A recurring character or group implicitly or explicitly referenced,
- OR a recurring concrete storyline element
  (such as a specific situation, decision, plan, accusation, object, or outcome).

The anchor must be specific enough that a viewer could recognize
that both events belong to the same storyline.

--------------------------------
INVALID CONNECTIONS (DO NOT USE)
--------------------------------
The following are NOT valid reasons to connect events:
- Shared setting or environment.
- Shared profession or role.
- Shared emotional tone or intensity.
- Shared activity type that appears frequently in the video.
- General world knowledge or genre conventions.

If the connection relies primarily on these,
the correct relation is "unrelated".

--------------------------------
SOFT CONTINUITY (RESTRICTED)
--------------------------------
Soft continuity is allowed ONLY when:
- Both events clearly belong to the same identifiable subplot,
- AND you can explicitly explain how Event B advances or revisits
  the situation introduced in Event A.

If you cannot answer:
"How does Event B specifically follow from Event A in this video?"
then you MUST choose "unrelated".

--------------------------------
CONFIDENCE RULES
--------------------------------
- Clear shared storyline element: 0.65–0.90
- Implicit but traceable continuation: 0.45–0.65
- Weak but defensible continuity: 0.35–0.45
- No clear story anchor: ≤ 0.30 and MUST be "unrelated"

--------------------------------
EVIDENCE RULES
--------------------------------
- Quotes must reference concrete storyline elements,
  not abstract descriptions or generic activities.
- The "reason" MUST explicitly name the shared story anchor.
- Reasons based on generic similarity are INVALID.

--------------------------------
INPUT
--------------------------------
Event A:
- task_id: {a_id}
- time_span: {a_start}-{a_end}
- topic: {a_topic}
- narrative_summary: {a_narr}

Event B:
- task_id: {b_id}
- time_span: {b_start}-{b_end}
- topic: {b_topic}
- narrative_summary: {b_narr}

Also provided:
- time_gap_seconds: {gap_s}

--------------------------------
OUTPUT
--------------------------------
Output a JSON object with this exact schema:
{{
  "src_task_id": "{a_id}",
  "dst_task_id": "{b_id}",
  "relation": "causes|enables|elaborates|contrasts|unrelated",
  "confidence": 0.0,
  "evidence": {{
    "src_quote": "",
    "dst_quote": "",
    "reason": ""
  }}
}}
"""


UPDATE_EVENT_TABLE_PROMPT = """
Update the Event Table (ET) after each clip.

ET represents ONE ongoing chapter-worthy local unit in the video.
It stores the cumulative state of that unit so far.

ET is NOT a per-clip summary.
ET is NOT a full-video summary.
ET should preserve a stable local anchor across adjacent clips whenever the same unit continues.

Input:
(A) pre_ET (or null)
(B) STM_k (current clip summary)

Return STRICT JSON:

{
  "event_identity": "...",
  "event_summary": "...",
  "entities": [...],
  "open_questions": [...],
  "delta": "..."
}

--------------------------------------------------
INITIALIZATION

If pre_ET is null:
- create a new local unit from STM_k
- event_identity should be short and local
- event_summary should describe the current unit state
- delta MUST be:

"SHIFT: NONE -> <event_identity>."

--------------------------------------------------
CORE IDEA

ET is the working state of the CURRENT chapter-worthy local unit.

A good ET should help preserve a video structure that is:
- locally coherent
- viewer-friendly
- navigable
- not fragmented by tiny visual variation
- not so broad that multiple meaningful parts are collapsed together

If the same unit continues:
- keep event_identity unchanged by default
- update event_summary by integrating prior state + new info
- do NOT rewrite ET to mirror STM_k
- do NOT broaden ET into a global or session-level summary

ET should stay local, stable, and moderately slow-changing.

--------------------------------------------------
EVENT IDENTITY

event_identity is the short title of the current local unit.

It should name the stable semantic unit currently unfolding in the video.

It must be:
- local
- stable
- specific enough to anchor the current unit
- not tied to one shot, object, or camera angle
- not so broad that it absorbs multiple meaningful parts of the video

Good identities are usually:
- a current task
- a phase
- a section
- a stage
- a segment focus
- a unit being explained, built, performed, presented, or completed

If several consecutive clips still serve the same chapter-worthy unit, keep the same identity.

--------------------------------------------------
WHEN TO CONTINUE

Prefer CONTINUE when:
- the same task, section, phase, project, stage, or focal unit is still unfolding
- the clip adds examples, sub-steps, local details, reactions, or new views of the same unit
- the visual focus changes but the same unit still explains the clip well

--------------------------------------------------
WHEN TO SHIFT

Use SHIFT only when the current clip is better understood as the start of a NEW local unit, such as:

- a new section, phase, stage, or named unit begins
- a different task, function, or focal activity becomes dominant
- a different artifact, outcome, segment focus, or block becomes central
- the video clearly moves into a new part such as setup, explanation, execution, showcase, recap, closing, award, or outro
- the clip contains a clear opener or reset for a new unit rather than just more detail of the old one
- a clear visual chapter/title card appears for the first time, such as a title screen, large on-screen heading, chapter card, or OCR-visible segment title
- a strong visible marker explicitly tells the viewer that a new part of the video has begun

A good SHIFT should make the video structure clearer and more viewer-friendly.

Do NOT use SHIFT for weak wording drift alone.
Do NOT keep everything under one broad umbrella just because it is loosely related.

--------------------------------------------------
DO NOT SHIFT FOR

- new examples of the same concept
- local sub-steps inside the same task / phase / project
- new objects used within the same ongoing unit
- camera, actor, or scene changes
- temporary inserts, montage, credits, subscribe prompts, or transition screens
  unless they clearly mark a real new unit

--------------------------------------------------
SUMMARY RULE

event_summary = compressed cumulative state of the same local unit.

Update it by:
- starting from pre_ET.event_summary
- integrating new relevant information from STM_k
- compressing back into a short state description

Keep only information still relevant to the SAME unit.
Do not let the summary become a broad session summary.

--------------------------------------------------
DELTA FORMAT

CONTINUE:
"CONTINUE: <how the same unit progresses>."

SHIFT:
"SHIFT: <old_identity> -> <new_identity>."

If SHIFT is used:
- <new_identity> must match event_identity
- SHIFT means a proposed unit change, not a final segmentation decision

--------------------------------------------------
LIMITS

event_identity <= 10 tokens
event_summary <= 80 tokens
entities <= 4
open_questions <= 2
delta <= 20 tokens

Return JSON only.
"""

EVENT_LINK_WITH_ET_PROMPT_AIO = """
# Role

You are a delayed-confirmation boundary gate for video chaptering.

A candidate boundary was proposed earlier at candidate_t.

Your task:
Given the previous ET state, the ancor segment that triggered the candidate,
and the later pending segment used for delayed confirmation,
decide whether the candidate boundary should be:

- REJECTED: KEEP the same chapter-worthy local unit as pre_ET
- ACCEPTED: SPLIT into a new chapter-worthy local unit

If splitting, output exactly ONE split point.

Your goal is to produce boundaries that are:
- structurally clear
- viewer-friendly
- navigable
- not fragmented by tiny local variation
- not so coarse that multiple meaningful parts are collapsed together

--------------------------------------------------
# Inputs

candidate_source: {candidate_source}
asr_confidence: {asr_confidence}

asr_chapter_context:
{chapter_context}

pre_ET:
{pre_et}

anchor_segment:
Summary: {anchor_summary}
Caption: {anchor_caption}
ASR lines:
{anchor_ASR}

pending_segment:
Summary: {pending_summary}
Caption: {pending_caption}
ASR lines:
{pending_ASR}

--------------------------------------------------
# Core Definition

SAME UNIT means:
The anchor segment and pending segment are still best understood
as continuation of the same chapter-worthy local unit represented by pre_ET,
even if examples, objects, views, or local steps change.

SPLIT means:
The anchor segment is better treated as the beginning of a new local unit,
and the pending segment continues that new unit rather than the old one.

A boundary should be accepted when it makes the video structure clearer for a viewer.

--------------------------------------------------
# Main Decision Principle

Use pre_ET as the old-unit anchor.
Use anchor_segment as the candidate-triggering evidence.
Use pending_segment as right-context confirmation.

Ask:

1) Does pre_ET still explain both segments well as one coherent local unit?
2) Or does anchor_segment already begin a different local unit,
   which pending_segment then continues?
3) Would splitting here create a clearer and more navigable chapter structure?

If the right-side evidence mainly continues the same underlying unit,
prefer KEEP.

If the right-side evidence confirms a different section / phase / stage / task / segment focus,
prefer SPLIT.

--------------------------------------------------
# Evidence Use

Use all available evidence, with these priorities:

1) clear structural or semantic change across pre_ET -> anchor_segment -> pending_segment
2) raw ASR when it is informative
3) pre_ET.event_identity and event_summary as the old-unit anchor
4) visual summaries / captions as supporting evidence

Important:
- ASR is strong evidence when it is informative, but not automatically decisive.
- pre_ET is a useful anchor, but not absolute truth.
- visual evidence alone is usually weak, unless it clearly marks a real new unit.

--------------------------------------------------
# ASR Confidence Rule

If asr_confidence is HIGH:
- treat ASR as strong evidence for section / task / topic / stage progression
- a clear semantic shift in ASR may support SPLIT even without explicit labels

If asr_confidence is LOW:
- treat ASR as weak evidence only
- do NOT split mainly because of lyrics, repeated sung lines, sparse speech,
  noisy ASR, or fragmented casual dialogue
- in LOW-confidence cases, require stronger ET or visual support before splitting

--------------------------------------------------
# Strong Split Evidence

Accept SPLIT when one or more of the following is clearly true:

A) anchor_segment already begins a new section, phase, stage, segment focus, or local unit,
   and pending_segment continues that new unit

B) the dominant task, function, outcome, or video focus changes,
   and pending_segment confirms the new direction

C) there is a clear visible or spoken opener, reset, title, marker, or transition
   for a new local unit

D) a clear visual chapter/title card appears for the first time, such as a title screen,
   chapter card, large on-screen heading, or OCR-visible segment title that explicitly marks a new part

E) keeping both segments under pre_ET would make the chapter structure too coarse or less clear

--------------------------------------------------
# Weak Evidence (Not sufficient alone)

Do NOT split based only on:
- camera or shot changes
- different people or objects
- new examples
- local sub-steps inside the same task / section
- wording drift between summaries
- short inserts, montage, credits, subscribe prompts

--------------------------------------------------
# Anti-Oversegmentation Rule

Do NOT split merely because:
- the new segment could have a slightly different short title
- the clip looks different
- a new object appears
- a small sub-step appears inside the same broader local unit

--------------------------------------------------
# Anti-Undersplitting Rule

Do NOT keep merely because:
- the scene is similar
- the same people remain
- the topic is broadly related
- everything belongs to the same broad activity domain

If anchor_segment starts a meaningfully new local unit and pending_segment confirms it,
split.

--------------------------------------------------
# Split Point Selection

If is_same_event = false, output exactly ONE split point.

Choose in this order:

1) the START timestamp of the raw ASR line in anchor_segment or pending_segment that clearly begins the new unit
2) otherwise the earliest explicit visible section onset, title card, chapter card, large heading, or OCR-visible segment title that marks the new unit
3) otherwise candidate_t

split_point.reason must be one of:
- structural_transition
- goal_shift
- anchor_shift
- entity_shift
- scene_break

Use:
- structural_transition for explicit section / phase / stage / opener / closing / title-card transition
- goal_shift for clear task / function reset
- anchor_shift for a new dominant local unit without explicit marker
- entity_shift only when a new main entity truly replaces the old core focus
- scene_break only for a real semantic jump in scene / time / location

--------------------------------------------------
# Output

Return STRICT JSON ONLY.

If KEEP:
{
  "rationale": "One short sentence.",
  "is_same_event": true,
  "split_point": []
}

If SPLIT:
{
  "rationale": "One short sentence.",
  "is_same_event": false,
  "split_point": [
    {
      "t": "HH:MM:SS",
      "reason": "anchor_shift|goal_shift|entity_shift|scene_break|structural_transition"
    }
  ]
}

Rules:
- If is_same_event=true -> split_point must be []
- If is_same_event=false -> exactly one split point
- Return JSON only
"""
