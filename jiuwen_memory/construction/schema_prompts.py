"""Prompts for the opt-in Entity Schema extractor.

The extraction guidance mirrors the high-recall Property extraction profile used by the local
LoCoMo evaluation. The output contract intentionally remains the minimal Agent Memory contract:
only ``entities[].properties[]`` are emitted and persisted; graph edges and episodes are outside
this feature.
"""

SCHEMA_SELECTION_FOR_GENERATION_PROMPT = """
You are a memory extraction schema expert. Given a dialogue, select the entity types and
properties that may contain retrievable facts.

Dialogue:
{dialogue_text}

Available entity types and properties:
{entity_schema}

Return exactly one JSON object:
{{
  "selected_entities": [
    {{
      "entity_type": "person",
      "relevant_properties": ["position_event", "hobby_activity", "plan_event"]
    }}
  ]
}}

Rules:
1. Use only entity types and property names shown above.
2. Never select episodes or higher-order properties.
3. default_property is retained automatically and need not be selected.
4. When unsure whether a property is relevant, include it; false negatives are worse than false
   positives at this stage.
5. Use ["all"] when most properties of an entity type could be relevant.
6. Include person whenever a person is explicitly mentioned or implied by a named speaker.
7. Select according to information actually present in the dialogue, not unrelated possibilities.
8. Output JSON only, without Markdown, reasoning, or commentary.
"""


ENTITY_GENERATION_PROMPT = """
# Role
You extract comprehensive, retrieval-ready entity properties from one dialogue batch under a
supplied Entity Schema. Factual completeness is the highest priority.

# Inputs
Entity Schema:
{entity_schema}

Dialogue timestamp used only when a supporting source has no message_time:
{dialogue_timestamp}

Input messages. Every marker contains its authoritative unit_id, message_time, role, and speaker:
{chat_chunk}

# Entity and property rules
1. Use only entity types and property names present in the supplied schema. Do not generate
   episodes, relationships, edges, or higher-order properties. Use
   default_property for a concrete retrievable fact that fits no more specific property.
2. Extract every concrete fact, state, event, plan, preference, recommendation, activity, and
   experience supported by the dialogue. When uncertain between omitting a useful fact and using
   default_property, use default_property.
3. Prefer literal, concrete facts to abstractions. Preserve exact names, dates, places,
   organizations, titles, products, quantities, frequencies, causes, recommendations, and image
   caption details. Never replace a specific term with a generic synonym.
4. Every value must be a complete, self-contained factual sentence containing an explicit subject,
   action or state, and all known context needed to understand it after retrieval. Never emit bare
   nouns, short fragments, or values such as "lost job", "first place", or "new hobby".
5. Do not store greetings, thanks, congratulations, questions, or other speech acts unless they
   independently reveal a concrete fact, plan, request, or recommendation.
6. Avoid duplicate properties that merely restate the same fact. A single source message may
   produce multiple properties when it contains distinct factual dimensions.

# Speaker identity
1. A marker such as speaker=Alice or [Alice]: identifies the real speaker. First-person facts on
   that message belong to Alice, not to a generic User, Assistant, Speaker, or Participant.
2. Preserve distinct named speakers as distinct entities. Never merge two explicit people because
   they have similar properties.
3. Named people mentioned by a speaker are separate entities when the dialogue states facts about
   them. Do not turn places, products, or organizations into person entities.

# Source attribution
1. Every property must contain source_unit_ids with one or more unit_id strings copied exactly
   from the input message markers that directly support the fact.
2. source_unit_ids is authoritative provenance. Never invent an id or derive it from position,
   message order, another property, or another message.
3. If multiple messages jointly support a fact, include every supporting source id. Do not attach
   unrelated sources merely because they are in the same batch.

# Fact and event time
1. properties[].time is the fact or event time, not ingest time, record time, dialogue time, or
   message time.
2. Resolve an explicit or relative fact-time expression against the message_time belonging to that
   property's supporting source_unit_ids. Use dialogue_timestamp only when the supporting message
   has message_time=unknown. Never use another message's timestamp.
3. Preserve the precision actually supported by the dialogue: YYYY, YYYY-MM, YYYY-MM-DD, or a
   complete ISO-8601 datetime. Never invent a day for a year- or month-level expression.
4. today and just/just now resolve to the supporting message date. next month resolves to the next
   calendar month. last week uses supported coarse month precision unless an exact date or weekday
   is stated. For a future plan, time is the planned execution time, not when the plan was stated.
5. If the fact has an explicit or relative time, value must contain the normalized fact/event time.
   Preserve the original relative expression and supporting message date when needed to make the
   result independently understandable.
6. If the fact itself has no explicit or relative time anchor, use an empty time string. Do not
   copy message_time or dialogue_timestamp into time. The system will add message-date/as-of
   context to the stored Property without fabricating t_event.
7. A different ISO date in value is allowed only when clearly labeled as a supporting message or
   reference date; it is not a second event time.

# Relative-time examples
- message_time=2023-01-20, "perform next month": time="2023-02"; value contains both
  "2023-02" and "next month from 2023-01-20".
- message_time=2023-01-29, "just launched": time="2023-01-29"; value contains both
  "2023-01-29" and "just launched".
- message_time=2023-07-09, "noticed last week": time="2023-07"; value contains both
  "2023-07" and "last week from 2023-07-09"; do not invent an exact day.
- message_time=2023-07-09, "started learning today": time="2023-07-09"; value contains both
  "2023-07-09" and "today".
- message_time=2023-07-15 (Saturday), "last Friday": use the most recent Friday before the
  message, 2023-07-14, not the Friday from the preceding calendar week.

# Zero fact loss check
Before answering, silently re-scan every substantive message and verify that the output preserves:
- every person and the correct speaker attribution;
- geographic names, organizations, item names, titles, brands, and quantities;
- activities, hobbies, skills, plans, achievements, health changes, and relationship states;
- recommendations, reasons, frequency information, and before-to-after changes;
- all explicit and relative time expressions with the correct supporting source message.

Do not omit a supported fact merely because another property summarizes part of the message.
Properties are the authoritative retrievable records.

# Output contract
Return exactly one bare JSON object without Markdown, reasoning, or commentary. The unique root
must explicitly contain entities. Return an empty entities array only when the input contains no
supported property fact.

{
  "entities": [
    {
      "name": "Jon",
      "entity_type": "person",
      "description": "Dance performer",
      "aliases": [],
      "properties": [
        {
          "property_name": "plan_event",
          "value": "In 2023-02 (next month from 2023-01-20), Jon plans to perform at a festival",
          "time": "2023-02",
          "source_unit_ids": ["unit-id-from-input"]
        }
      ]
    }
  ]
}
"""


SINGLE_ENTITY_MERGE_PROMPT = """
You are a memory integration expert. Decide whether this newly extracted entity should CREATE a
new entry or UPDATE an existing one.

## New Entity
- Name: {entity_name}
- Type: {entity_type}
- Description: {entity_description}

## Existing Entity Candidates (from vector search)
{existing_entities}

## Decision Criteria

### Use UPDATE when (PREFERRED — default choice unless clearly wrong):
1. The entity name matches or is similar to an existing candidate (same person, thing, or event)
2. The entity could plausibly be the same real-world entity as an existing candidate
3. Same name with additional context. For example, new facts about Jon's dancing and old facts
   about Jon's job normally describe the same person.
4. The target_entity name MUST be one of the candidates listed above

### ⚠️ CRITICAL: Base-Name Matching Rule
When the new entity and a candidate share the same base name (the part before a parenthetical
qualifier), they are almost certainly the same entity even when qualifiers differ.
- "Toby(German Shepherd)" and "Toby(golden retriever)" → **UPDATE**.
- "Fox Hollow(hiking trail)" and "Fox Hollow(nature reserve)" → **UPDATE**.
- "Ferrari(sports car)" and "Ferrari(488 GTB)" → **UPDATE**.

**Why:** Parenthetical qualifiers are descriptive annotations, not identity-defining features.
Conflicting qualifiers usually indicate imprecise descriptions rather than distinct entities.

**Same-speaker context strengthens merge confidence:** If both observations occur in conversations
between the same speakers, that is strong evidence for the same real-world entity.

### Use CREATE only when:
1. No candidate in the existing list could possibly match this entity
2. Explicit evidence identifies different entities, such as different full names and locations.
3. The entity type is fundamentally incompatible, such as a person versus an organization.
4. Different parenthetical qualifiers alone are not sufficient evidence for CREATE.

## Output Format (JSON object, NOT array)

For CREATE:
```json
{{
    "action": "create",
    "relation_candidates": [
        {{"target_entity": "Existing entity name", "relation": "Relationship description"}}
    ]
}}
```

For UPDATE:
```json
{{
    "action": "update",
    "target_entity": "Existing entity name to update (MUST be in candidate list above)"
}}
```

## Rules
1. Output exactly ONE decision
2. When uncertain, prefer UPDATE for the same name unless explicit evidence says otherwise.
3. Use CREATE only with concrete evidence of a genuinely different identity.
4. Same base name and entity type means UPDATE; qualifiers do not justify CREATE.
5. For relation_candidates, only include entities with clear relationships
6. If no clear relations exist, relation_candidates can be empty []
7. For UPDATE, target_entity MUST exactly match a name from the candidate list

Output only JSON, no extra text.
"""

AGENT_MEMORY_ENTITY_MERGE_APPENDIX = """
## Agent Memory identity boundaries (take precedence)
1. Generic roles such as User, Assistant, Speaker, or Participant are not aliases for a named
   person. Never UPDATE a named entity to a generic-role candidate or the reverse.
2. Different explicit speaker labels identify different people and must CREATE, even if their
   properties are semantically similar.
3. Exact explicit speaker names are stronger identity evidence than semantic similarity.
"""


__all__ = [
    "AGENT_MEMORY_ENTITY_MERGE_APPENDIX",
    "ENTITY_GENERATION_PROMPT",
    "SCHEMA_SELECTION_FOR_GENERATION_PROMPT",
    "SINGLE_ENTITY_MERGE_PROMPT",
]
