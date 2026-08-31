"""Prompts for the opt-in Entity Schema extractor."""

SCHEMA_SELECTION_FOR_GENERATION_PROMPT = """
You select the smallest useful subset of an entity schema for one dialogue.

Dialogue:
{dialogue_text}

Available entity types and properties:
{entity_schema}

Return exactly one JSON object:
{{
  "selected_entities": [
    {{
      "entity_type": "person",
      "relevant_properties": ["occupation", "employer"]
    }}
  ]
}}

Rules:
1. Use only entity types and property names shown above.
2. Use ["all"] when every property of one entity type is relevant.
3. Include a type when the dialogue contains a supported fact about an instance of that type.
4. Do not select episodes or invent types and properties.
5. Output JSON only, without Markdown or commentary.
"""


ENTITY_GENERATION_PROMPT = """
You extract self-contained property facts under an explicit entity schema.

Entity schema:
{entity_schema}

Dialogue timestamp for resolving explicit relative-time expressions:
{dialogue_timestamp}

Input messages. Each message marker contains its authoritative unit_id:
{chat_chunk}

Return exactly one JSON object with this shape:
{
  "entities": [
    {
      "name": "Alice",
      "entity_type": "person",
      "properties": [
        {
          "property_name": "occupation",
          "value": "On 2024-05-03, Alice started working as a software engineer at Acme.",
          "time": "2024-05-03",
          "source_unit_ids": ["the exact supporting unit_id"]
        }
      ]
    }
  ]
}

Rules:
1. Use only entity types and properties present in the supplied schema.
2. Every property value must be a complete factual statement with an explicit subject.
3. Preserve concrete names, dates, places, quantities, and other retrievable details.
4. Every property must contain one or more exact source_unit_ids copied from the input markers.
5. A speaker label such as `speaker=Alice:` or `[Alice]:` is an identity boundary. Attribute
   first-person facts to that named speaker, never to a generic User or Speaker.
6. `time` is optional fact/event-time enrichment. Use YYYY, YYYY-MM, YYYY-MM-DD, or an ISO-8601
   datetime when the fact itself has a supported time anchor; otherwise use an empty string.
7. Do not copy the dialogue timestamp into a fact that has no time anchor.
8. Do not output message_mapping, edges, episodes, delete commands, or higher-order properties.
9. Output one complete JSON object only, without Markdown or commentary.
"""


__all__ = [
    "ENTITY_GENERATION_PROMPT",
    "SCHEMA_SELECTION_FOR_GENERATION_PROMPT",
]
