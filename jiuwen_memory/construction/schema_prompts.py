"""MindMemOS English schema prompts plus Agent Memory compatibility appendices.

The base prompt constants below are kept in sync with the local MindMemOS prompt modules.
Agent Memory appends the explicit compatibility contracts at each call site because its public
persistence model is MemoryUnit-based rather than MindMemOS' Entity/Property stores.
"""

# ruff: noqa: E501

SCHEMA_SELECTION_FOR_GENERATION_PROMPT = (
    "You are a memory extraction schema expert. Given a dialogue, select which entity types and their dynamic "
    "properties are relevant for extracting structured memories.\n\n"
    """Dialogue:
{dialogue_text}

Speaker note: Lines may be formatted as `speaker=Name: ...` for named-speaker dialogue.
Treat `Name` as the real speaker of that line; first-person statements in that line belong to `Name`, not
automatically to the user.

Available Entity Types and Properties:
{entity_schema}

Select the entity types and properties that are relevant to the information in this dialogue.

Output Format (JSON):
{{
    "selected_entities": [
        {{
            "entity_type": "person",
            "relevant_properties": ["position_event", "hobby_activity", "plan_event"]
        }},
        {{
            "entity_type": "animal",
            "relevant_properties": ["all"]
        }}
    ],
    "reasoning": "Brief explanation of why these types and properties were selected"
}}

Rules:
1. ALWAYS include "episodes" entity type (it will be added automatically, no need to list it)
2. "default_property" is ALWAYS included for every selected entity type (no need to list it)
3. When unsure whether a property is relevant, INCLUDE it — false negatives are worse than false positives
4. Use ["all"] to keep all properties of an entity type when most properties could be relevant
5. Only EXCLUDE properties that are clearly irrelevant to the dialogue content
6. ALWAYS include "person" entity type if any person is mentioned or implied in the dialogue
7. Focus on what information the dialogue CONTAINS, not what it might theoretically relate to
"""
)


ENTITY_GENERATION_PROMPT = """
# Role Definition
You are a professional entity and relationship extraction expert, responsible for extracting comprehensive and
accurate structured memory information from dialogues.

# Task Description
You will receive:
1. Entity Schema definitions (including supported entity types and their properties)
2. A segment of dialogue text

Your goal is to extract ALL entities mentioned in the dialogue that conform to the schema, along with all their
relevant properties. Be thorough - do not omit any potentially useful information.

# Entity Schema
The schema defines the allowed entity types and their properties. Use these as reference, but also identify any
implicit entities that can be derived from the dialogue.

{entity_schema}

# Extraction Principles

## 0. Speaker Attribution
- Dialogue lines may use `speaker=Name: ...` for named-speaker conversations.
- Treat `Name` as the actual speaker of that line. First-person statements such as "I moved to Boston" belong to
`Name`, not automatically to the user.
- When converting dialogue to objective property values, use the explicit speaker name as the subject whenever
available.

## 1. Be Comprehensive - Do Not Miss Entities
- Extract EVERY entity mentioned in the dialogue (people, places, organizations, events, facts, etc.)
- If uncertain whether something should be an entity, include it anyway
- Look for: persons, locations, organizations, events, activities, projects, products, documents, conversations,
facts, etc.

## 2. Property Values - Keep Original Meaning & Concrete Details
- **Preserve the original phrasing** from the dialogue as much as possible
- **PRIORITIZE CONCRETE FACTS over abstract summaries**
  - Extract "lost job in January 2023" rather than "career transition"
  - Extract "visited Rome and Paris" rather than "traveled internationally"
  - Extract "met at coffee shop on Main Street" rather than "social meeting"
- **Preserve ALL specific details**: exact dates, names, quantities, locations, amounts
- When extracting property values, use the speaker's exact words or paraphrases that maintain the original nuance
- Do NOT summarize or generalize - keep specific details
- Example: If user says "I'm working on the Q3 sales report with Li Ming and Wang Hua", extract both colleagues' names
- **Factual Accuracy Priority**: When multiple interpretations exist, choose the most literal and concrete one

## Property Extraction Strategy: Factual Record vs Analytical Summary

**IMPORTANT**: Factual completeness is ALWAYS the top priority. The analytical summary approach below is an
ADDITIONAL capability for certain property types — it does NOT reduce the requirement to capture all concrete facts.

Different property types require different extraction approaches:

### Factual Record Properties (preserve full original details)
These properties record concrete events and facts. Preserve ALL specific details verbatim:
- `location_event`, `position_event`, `health_event`, `achievement_event`, `travel_event`, `business_event`,
`education_degree`, `education_field`, `plan_event`, `reading_activity`, `social_activity`, `hobby_activity`,
`experience`, `relationship_status`, `identity`, `family_plan`
- **Strategy**: Keep original phrasing, exact names, dates, locations, quantities. Do not summarize or abstract.

### Analytical Summary Properties (distill key conclusions IN ADDITION to facts)
These properties capture preferences, attitudes, opinions, and behavioral patterns. In addition to preserving
concrete details, distill the DEFINITIVE CONCLUSION into a retrieval-friendly assertion:
- `preference`, `preference_evolution`, `opinion`, `attitude_change`, `habit_event`, `mood_event`,
`career_interest`, `financial_status`, `advice_given`
- **Strategy**:
  - Still preserve all concrete details (specific names, items, activities mentioned)
  - Additionally, frame the value as a clear assertion: "{name} prefers/believes/likes/dislikes {specific_thing}"
  - For preference changes: explicitly state direction "changed from X to Y"
  - For opinions: state the conclusion directly "{name} believes X because Y"
  - Only record conclusions CLEARLY supported by the dialogue. Do NOT infer uncertain attitudes
  - Include the specific subject/object (e.g., "prefers thriller novels over romance" not just "reading preferences
  changed")

**Examples of Analytical Summary extraction:**
- Dialogue: "I used to love going to big concerts but honestly after COVID I just prefer intimate acoustic shows
now, the energy is so much better"
  - GOOD preference_evolution: "As of 2024-03, Alex's music preference evolved from large concerts to intimate
  acoustic shows, finding the energy better in smaller venues"
  - BAD (too narrative): "As of 2024-03, Alex mentioned that after COVID they changed their mind about concerts and
  now like smaller ones"
- Dialogue: "I've been really getting into index fund investing lately, moved most of my savings out of individual
stocks"
  - GOOD financial_status: "As of 2024-03, Alex shifted investment strategy from individual stocks to index funds"
  - BAD (too vague): "As of 2024-03, Alex talked about changing their investment approach"
- Dialogue: "Honestly I think remote work is way more productive, I get so much more done without the office
distractions"
  - GOOD opinion: "As of 2024-03, Alex believes remote work is more productive than office work due to fewer
  distractions"
  - BAD (uncertain inference): "As of 2024-03, Alex might prefer working from home" — this is too weak; the dialogue
  clearly states a firm opinion

## 3. No Duplicate Information (Unless Necessary)
- Each piece of information should appear in ONE property field
- If the same information is relevant to multiple aspects, you may include it in different fields with different
FOCUSES:
  - **description**: Brief summary of the entity (1-2 sentences)
  - **property values**: Detailed, specific information with original phrasing
- Example:
  - "Alice joined the company in 2020 as a software engineer, then became senior engineer in 2022"
  - description: "Software engineer at the company"
  - position_event: "On 2020, Alice joined the company as a software engineer"
  - position_event: "On 2022, Alice was promoted to senior engineer" (separate time point, not duplicate!)

## 4. Information Completeness & Critical Detail Preservation
- Ensure all important details from the dialogue are captured
- Include: who, what, when, where, why, how details
- If dialogue mentions a detail but no suitable property exists, use the **default_property** as a catch-all
- Every property value MUST be a **semantically complete statement** — a reader should understand the full fact
without needing other properties

**CRITICAL DETAIL PRESERVATION RULES**:
- **Person Names**: Always include full names of people mentioned (e.g., "worked with Amy's colleague, Rob" not just
"worked with a colleague")
- **Special Nouns & Entities**: Preserve all proper nouns, brand names, place names, organization names exactly as
mentioned
- **Item Names**: Include specific product names, book titles, movie names, restaurant names, tattoo designs, game
names, etc.
- **Quantities & Numbers**: Record exact numbers, amounts, prices, percentages, dates, times (e.g., "ordered 3
pizzas" not "ordered pizzas")
- **Specific Activities**: Use precise activity descriptions (e.g., "practiced hot yoga" not just "exercised")
- **Time Points**: Include all specific times mentioned (e.g., "at 3:30 PM", "every Tuesday", "twice a week")
- **Frequency Information**: Record recurring activities and their frequency (e.g., "goes to yoga class every
Tuesday and Thursday")
- **Patterns & Habits**: Note patterns of behavior and habitual actions
- **Causal Relationships**: Preserve "because", "due to", "as a result of" connections between facts
- **Suggestions & Recommendations**: When someone suggests or recommends something (e.g., "You should try X", "I
recommend Y"), extract the specific suggestion with context
- **Photo/Image Descriptions**: When someone describes a photo, image, or visual content, capture the described details
- **Motivational Quotes & Cultural References**: Preserve specific quotes, catchphrases, or cultural references
mentioned (e.g., a speaker quoting a famous person's catchphrase as motivation)
- **Concrete Items & Designs**: Extract specific item descriptions (e.g., "sunflower tattoo design", "blue velvet
dress", "acoustic guitar")

**⚠️ IMAGE CAPTION PRESERVATION RULE (CRITICAL — MANDATORY):**
- When a message contains image content (indicated by [Shared image: ...] or [Image context: ...] in the text), the
COMPLETE original image caption MUST be preserved in the property value
- Format: Include the original caption in brackets: [Original caption: ...]
- Example property value: "On 2024-03-20, Jon shared a photo of dancers performing on a stage with a red background
[Original caption: a photo of a group of dancers on stage], representing his students' progress"
- Do NOT paraphrase, abbreviate, or omit the original caption under any circumstances

**⚠️ ALIAS / ALTERNATIVE NAME PRESERVATION RULE (CRITICAL — MANDATORY):**
- When different names, nicknames, or alternative terms refer to the SAME entity in the conversation, ALL variants
MUST be preserved using parentheses in property values, entity names, and descriptions
- This includes: brand names vs product names, full names vs nicknames, formal names vs slang, game titles vs
platform names, different language terms for the same thing
- **Item Type Annotation**: For any named item, product, game, toy, pet, or entity whose category is not obvious
from the name alone, annotate with its specific type/category in parentheses. The more specific, the better.
  - Example: "Labubu(a PopMart designer toy)", NOT just "Labubu"
  - Example: "Toby(golden retriever puppy)", NOT just "Toby"
  - Example: "Catan(a strategy board game)", NOT just "Catan"
  - Example: "Monster Hunter: World(Nintendo Wii game)", NOT just "Monster Hunter: World"
- Format: "primary_name(type/alias)" or "entity(alternative_description)"
- Example: "On 2024-03-20, Alex played a PS5 game(Star Wars) with Mary" — preserve both the platform category and
the specific game title
- Example: Entity name "Jon(John)" when both names are used in conversation
- Example: "As of 2024-03-20, Alex adopted a dog named Toby(golden retriever)" — preserve breed as alias
- This ensures the system can match queries regardless of which name variant the user searches with

**SEMANTIC COMPLETENESS RULE** (CRITICAL):
- BAD: "lost job" → GOOD: "On January 2023, Alex lost his job at the delivery company DoorDash"
- BAD: "sunflower" → GOOD: "On March 15, Alex expressed interest in getting a sunflower tattoo design"
- BAD: "performed well" → GOOD: "On July 23, Alex's dance team performed a contemporary piece called 'Finding
Freedom' and won first place at the summer dance festival"
- Every value should be a self-contained fact that includes subject, action, and all known contextual details

## PROPERTY VALUE QUALITY GATES (MANDATORY - system will reject values that fail)
Every property value MUST pass ALL of these checks before acceptance:
1. **Subject present**: The value MUST contain the entity's name or an unambiguous subject reference
   - REJECT: "is passionate about painting" → ACCEPT: "Caroline is passionate about painting"
   - REJECT: "learning piano" → ACCEPT: "Caroline is learning the piano as a creative activity"
   - REJECT: "lost job" → ACCEPT: "Alex lost his job at DoorDash in January 2023"
2. **Self-contained**: A reader must understand the full fact without seeing other properties or context
   - REJECT: "sunflower" → ACCEPT: "Alex expressed interest in getting a sunflower tattoo design"
   - REJECT: "acoustic guitar" → ACCEPT: "Caroline started playing acoustic guitar about five years ago"
   - REJECT: "painting and drawing" → ACCEPT: "Caroline is passionate about painting and drawing as creative outlets"
3. **No orphan fragments**: Never store bare nouns, adjectives, short verb phrases, or sentence fragments
   - REJECT: "great performance", "first place", "new hobby"
   - ACCEPT: Full sentences with subject + verb + object/complement
4. **No bare speech acts**: Do NOT store property values that only record someone asking a question, greeting,
thanking, congratulating, or making small talk — unless the speech act itself reveals a new fact.
   - REJECT: "Andrew asked Audrey if her dogs enjoy going on hikes" — this is just a question, no factual content
   - REJECT: "Audrey congratulated Andrew on his new job" — pure social interaction, no new fact
   - REJECT: "Andrew said he is excited about the trip" — vague emotional expression without specific detail
   - ACCEPT: "Andrew asked Audrey to recommend a hiking trail near Fox Hollow" — reveals a specific plan/location
   - ACCEPT: "Audrey suggested Andrew try the Blue Ridge trail for his first hike with Toby" — contains a concrete
   recommendation
   - **Rule of thumb**: If removing the speech verb ("asked", "said", "mentioned") leaves no retrievable fact, do
   NOT store it.
5. **Timestamp context in value** ⚠️ MANDATORY FOR EVERY VALUE: **ALL** property values MUST contain a date
reference, no exceptions
   - If the dialogue mentions a specific date → use it: "On 2023-07-17, Caroline got promoted to senior designer"
   - If the dialogue mentions relative time → use natural form: "Last week from 2023-05-08, Caroline attended the
   LGBTQ support group"
   - If NO time is mentioned in the dialogue → use the dialogue timestamp as default: "As of 2023-05-08, Caroline is
   transgender and a member of the LGBTQ community"
   - Use "On YYYY-MM-DD" for events/actions, "As of YYYY-MM-DD" for states/identities/traits
   - REJECT: "Caroline is transgender" → ACCEPT: "As of 2023-05-08, Caroline is transgender and a member of the
   LGBTQ community"
   - REJECT: "got promoted" → ACCEPT: "On 2023-07-17, Caroline got promoted to senior designer"
   - REJECT: "Caroline loves painting" → ACCEPT: "As of 2023-05-08, Caroline loves painting as a creative outlet"

## 5. Greedy Complete Coverage ⚠️ CRITICAL
Each property value MUST greedily cover ALL substantive information from the corresponding part of the original
message. Do not extract only a fragment and discard the rest.

**⚠️ ZERO FACT LOSS CHECK**: After extraction, re-scan every message. For each message containing substantive
information, verify that ALL of the following are captured in at least one non-episode entity property:
- **Geographic names** (countries, cities, states, regions, landmarks) — e.g., "Phuket", "Minnesota", "Stamford"
- **Specific suggestions/recommendations** one speaker makes to the other — e.g., "install a bird feeder", "try
cooking dog treats for the dogs"
- **Activities, hobbies, and skills** mentioned even casually — e.g., "surfing", "yoga retreat", "cat-themed card game"
- **Named items, gifts, and objects** — e.g., "yellow coffee cup with handwritten message", "forest scene painting"
- **Relationship identifiers** — preserve exactly as stated (e.g., "partner", "sister", "pet") without guessing or
re-labeling

**Rules**:
1. **Every property value must be complete — no omissions allowed**: If a message mentions a method, location, time,
schedule, or any other detail alongside the main fact, ALL of these must appear in the property value. Do not strip
away qualifying details.
2. **Every message must be independently and fully extracted**: Each message containing substantive information must
be fully captured in the appropriate non-episode entity properties. The existence of an episode entity does NOT
exempt you from extracting the same information into person/org entities.
3. **One message → multiple properties when needed**: If a single message covers multiple factual dimensions (time,
place, method, target, etc.), split them into separate property values rather than merging into one generic summary.
4. **Preserve original terminology**: Specific adjectives, proper nouns, method names, brand names, and activity
type names (e.g., "positive reinforcement", "glazing techniques", "Lotus Garden") must be kept verbatim. Never
substitute with synonyms or generic terms.
5. **Description-Property Consistency Rule** ⚠️ MANDATORY: For every non-episode entity, ALL substantive information
mentioned in the entity's `description` field MUST be fully covered by at least one property value. The properties
together must contain EVERY fact that the description summarizes — the description is a brief overview, but
properties are the authoritative record. If you write something in description, there MUST be a corresponding
property capturing that information in full detail.
   - BAD: description says "Person who moved to Shanghai and works at Alibaba on cloud project" but properties only
   contain location_event and miss the work/project info
   - GOOD: description says "Person who moved to Shanghai and works at Alibaba on cloud project" and properties
   contain location_event (the move), position_event (works at Alibaba), AND experience (cloud project with
   teammates)

**GREEDY COVERAGE EXAMPLES**:

Message: "2024-03-20: I signed up for a positive reinforcement dog training class last week, it's at the community
center on Oak Street every Saturday morning"
BAD extraction (incomplete):
  - training_event: "On 2024-03-13, Alex signed up for a dog training class"  ← MISSING: training method, location,
  schedule
GOOD extraction (complete):
  - training_event: "Last week from 2024-03-20, Alex signed up for a positive reinforcement dog training class at
  the community center on Oak Street, held every Saturday morning"

Message: "2024-03-20: I've been making YouTube videos about pottery since last July, and I just finished editing one
about glazing techniques yesterday"
BAD extraction (partial):
  - creative_work: "As of 2024-03-20, Alex makes YouTube videos about pottery"  ← MISSING: start time July, specific
  video topic, completion time
GOOD extraction (complete, split into two time points):
  - creative_work: "Since July 2023, Alex has been making YouTube videos about pottery"
  - creative_work: "On 2024-03-19, Alex finished editing a YouTube video about glazing techniques"

Message: "2024-03-20: My friend recommended this amazing Thai restaurant called Lotus Garden near the university,
they have the best pad thai"
BAD extraction:
  - recommendation_given: "As of 2024-03-20, Alex likes Thai food"  ← MISSING: restaurant name, location, specific
  dish, that it was a recommendation
GOOD extraction:
  - recommendation_given: "As of 2024-03-20, Alex's friend recommended a Thai restaurant called Lotus Garden near
  the university, known for their pad thai"

## 6. Entity Type Selection & Default Property Usage
- **Entity type (`entity_type`) MUST be one of the types defined in the schema** (e.g., person, organization). NEVER
invent new entity types. Do NOT generate "episodes" type entities — the system creates them separately.
- If an entity does not perfectly match any entity type in schema, choose the **closest matching type**. For
example, a pet or named animal should use "animal"; a fictional character or public figure should use "person"; a
club or team should use "organization".
- **`default_property` is a PROPERTY NAME, NOT an entity type.** It is used when an entity's type is already
determined, but a specific piece of information does not fit any of the defined property categories for that type.
- Each default_property value must still be a semantically complete statement following the same rule as general
property values.

# Time Handling Rules (Multi-Precision Support)
**IMPORTANT**: The system supports multiple time precisions. Choose the appropriate precision based on information
provided in the dialogue:

## Supported Time Formats (max precision: DAY)
1. **Year precision**: `2023` - only year is known
2. **Month precision**: `2023-05` - year and month are known
3. **Day precision**: `2023-05-24` - complete date is known

## Time Extraction Principles
1. **Preserve original precision** - DO NOT fill in unknown information
   - Dialogue says "in 2023" → use `2023`
   - Dialogue says "in May 2023" → use `2023-05`
   - Dialogue says "on May 24, 2023" → use `2023-05-24`

2. **Explicit time information**: Prioritize time explicitly mentioned in dialogue
   - "I graduated in 2023" → `2023`
   - "Joined in May 2023" → `2023-05`
   - "on March 19, 2024" → `2024-03-19`

3. **Relative time inference**: Infer based on dialogue timestamp. Use COARSER precision when uncertain.
   - The dialogue timestamp includes the day of the week (e.g., "2023-07-15 13:51:00 (Saturday)"). Use this to
   calculate relative dates precisely.
   - **CRITICAL — "last [weekday]" calculation rule**:
     - "last Friday" means the MOST RECENT Friday BEFORE the conversation date, NOT the Friday of the previous
     calendar week
     - Step-by-step: (1) Note the conversation day of week from the timestamp, (2) Count backwards to find the
     nearest target weekday, (3) That is the answer
     - Example: Timestamp is "2023-07-15 (Saturday)", speaker says "last Friday" → July 14 (1 day back), NOT July 7
     - Example: Timestamp is "2023-09-13 (Wednesday)", speaker says "last Monday" → Sept 11 (2 days back), NOT Sept 4
     - Example: Timestamp is "2023-02-09 (Thursday)", speaker says "last Wednesday" → Feb 8 (1 day back), NOT Feb 1
   - **"last weekend" rule**: means the most recent Saturday-Sunday before the conversation date
     - Example: Timestamp is "2023-05-24 (Wednesday)", "last weekend" → May 20-21, NOT May 13-14
   - Dialogue time is 2024-03-20, user says "yesterday" → time field: `2024-03-19`, value: "On 2024-03-19
   (yesterday), ..."
   - Dialogue time is 2024-03-20, user says "last week" → time field: `2024-03`, value: "Last week from 2024-03-20, ..."
   - Dialogue time is 2024-03-20, user says "last month" → time field: `2024-02`, value: "Last month from
   2024-03-20, ..."
   - Dialogue time is 2024-03-20, user says "last year" → time field: `2023`, value: "Last year from 2024-03-20, ..."
   - **NEVER fabricate a specific day from a vague relative expression** — if "last week" is said, don't guess which
   exact day

4. **Default to dialogue timestamp when time not mentioned**:
   - If no time is mentioned at all, use `{dialogue_timestamp}` as default (day precision max)
   - Strip any time-of-day component: "2024-03-20 14:30:00" → use `2024-03-20`

5. **Forbidden behaviors**:
   - ❌ DO NOT use "unknown" or any placeholder
   - ❌ DO NOT use datetime with hours/minutes/seconds (e.g., "2023-05-24 14:30:00")
   - ❌ DO NOT expand "2023" to "2023-01-01"
   - ❌ DO NOT expand "2023-05" to "2023-05-01"
   - ❌ DO NOT use descriptive time expressions like "before 2023", "after 2023-05", "around 2023"
   - ❌ DO NOT use "As of 2023-08-21 16:29:00, ..." format in values
   - ✅ ONLY use exact formats: `2023`, `2023-05`, `2023-05-24`
   - ✅ Keep the precision level provided in dialogue

# Property Value Rules
1. All property values MUST be strings - never use lists or dicts
2. Use the format specified in schema - each property has an example format, follow it
3. Only use properties defined in schema
4. **Keep original phrasing from dialogue** - preserve specific words, names, and details
5. **Concrete Details Priority**: When extracting information, prioritize concrete, specific facts over abstract
summaries
   - Extract "lost job in January 2023" rather than "career transition"
   - Extract "visited Rome and Paris" rather than "traveled internationally"
   - Preserve specific dates, amounts, names, and locations exactly as mentioned
   - Use literal quotes when speakers use specific phrases
6. **Factual Precision**: Avoid generalizations that could lose important distinctions
   - "started dance studio because lost job" ≠ "pursuing passion for dance"
   - Both may be true, but the causal relationship is more specific and valuable
7. **Time-Sensitive Information**: Give extra care to temporal details as they are crucial for retrieval accuracy
   - For relative time mentions (yesterday, last week, etc.), preserve the original relative expression naturally in
   the value
   - Format: "Last week from {dialogue_timestamp}, ..." or "On 2024-03-14 (yesterday), ..."
   - Example: "Last week from 2024-03-20, Alex had a great time talking about childhood memories"
   - NEVER use format like "As of 2024-03-20 14:30:00, ..." — no time-of-day precision in values
8. **Frequency and Recurring Information**: Always preserve patterns and frequency details
   - "every Tuesday and Thursday" not just "regularly"
   - "called three times during the conversation" not just "called multiple times"
   - "usually has coffee at 8 AM" includes both the activity and timing pattern

# Message Mapping Requirements ⚠️ Critical
Before generating the final answer, you must output a message mapping dictionary `message_mapping` explaining how
each message maps to which entity's properties.

## Mapping Format Requirements
```json
{
  "message_mapping": {
    "0": {
      "mappings": [
        {"entity": "Entity Name", "property": "Property Name"},
        {"entity": "Entity Name", "property": "Property Name"}
      ],
      "reason": "Mapping reason explanation"
    },
    "1": {
      "mappings": [
        {"entity": "Entity Name", "property": "Property Name"}
      ],
      "reason": "Mapping reason explanation"
    },
    "2": {
      "mappings": [],
      "reason": "No mapping reason explanation (e.g., pure congratulations, no substantial information)"
    }
  },
  "mapping_comments": "Overall mapping explanation"
}
```

## Mapping Principles
1. **Index reference**: Use message indices "0", "1", "2", "3" etc. to reference messages, indices must be
consecutive starting from 0
2. **Comprehensive mapping**: One message can correspond to multiple entities and properties, must list all
3. **Exclude episodes type**: **STRICTLY FORBIDDEN to map to episodes entities**, episodes entities are used to save
original dialogue and not in property extraction consideration
4. **Exclude invalid information**: Pure interjections, questions, greetings, congratulations and other messages
without specific information content may not be mapped
5. **Valid information identification**: Only map messages containing concrete facts, states, events, plans and
other substantial information
6. **Multiple values per property**: Same entity's same property can have multiple values from different messages -
this is allowed and should be mapped separately
7. **Reason explanation**: Every message must have a reason field explaining why it maps to these properties (or why
it doesn't map)

# Output Format
Output clean JSON with `message_mapping`, `entities` and `edges` top-level fields.
- **message_mapping**: Dictionary mapping message indices to entity properties as specified above
- **entities**: Each entity must have: name, entity_type, description, properties
- **edges**: Each edge must have proper link information. Edge `link_description` must describe a **factual
relationship** (e.g., "works at", "owns", "lives in", "adopted from"), NOT a speech act (e.g., "asked about",
"mentioned", "talked about", "congratulated on"). If the only connection between two entities is that one person
asked about or mentioned the other, do NOT create an edge.
- Each property must have: property_name, value, time
- Use string values only for all properties

# Example

## Input Dialogue (timestamp: 2024-03-20)
Alice: I moved from Beijing to Shanghai yesterday, started working at Alibaba in 2023.
Bob: Congratulations! How's the work?
Alice: It's great! I'm working with Li Ming and Wang Hua on the cloud migration project.

## Correct Output (note time precision — do NOT generate episodes entities, the system handles them separately)
```json
{
  "message_mapping": {
    "0": {
      "mappings": [
        {"entity": "Alice", "property": "location_event"},
        {"entity": "Alice", "property": "position_event"}
      ],
      "reason": "Contains concrete facts about location change and work history"
    },
    "1": {
      "mappings": [],
      "reason": "Pure congratulations and question without substantial factual information"
    },
    "2": {
      "mappings": [
        {"entity": "Alice", "property": "experience"}
      ],
      "reason": "Contains information about current project work and colleagues"
    }
  },
  "entities": [
    {
      "name": "Alice",
      "entity_type": "person",
      "description": "Person who moved from Beijing to Shanghai",
      "properties": [
        {
          "property_name": "location_event",
          "value": "On 2024-03-19 (yesterday), Alice moved from Beijing to Shanghai",
          "time": "2024-03-19"
        },
        {
          "property_name": "position_event",
          "value": "In 2023, Alice started working at Alibaba",
          "time": "2023"
        },
        {
          "property_name": "experience",
          "value": "As of 2024-03-20, Alice is working with Li Ming and Wang Hua on cloud migration project",
          "time": "2024-03-20"
        }
      ]
    },
    {
      "name": "Alibaba",
      "entity_type": "organization",
      "description": "Company where Alice works",
      "properties": []
    },
    {
      "name": "Li Ming",
      "entity_type": "person",
      "description": "Alice's colleague at Alibaba",
      "properties": []
    },
    {
      "name": "Wang Hua",
      "entity_type": "person",
      "description": "Alice's colleague at Alibaba",
      "properties": []
    },
    {
      "name": "Cloud Migration Project",
      "entity_type": "project",
      "description": "Project Alice is working on with Li Ming and Wang Hua at Alibaba",
      "properties": [
        {
          "property_name": "project_member",
          "value": "As of 2024-03-20, project members are Alice, Li Ming, Wang Hua",
          "time": "2024-03-20"
        }
      ]
    }
  ],
  "edges": [
    {
      "link_entity1_name": "Alice",
      "link_entity2_name": "Alibaba",
      "link_description": "works at"
    },
    {
      "link_entity1_name": "Alice",
      "link_entity2_name": "Cloud Migration Project",
      "link_description": "working on"
    },
    {
      "link_entity1_name": "Li Ming",
      "link_entity2_name": "Alibaba",
      "link_description": "works at"
    },
    {
      "link_entity1_name": "Wang Hua",
      "link_entity2_name": "Alibaba",
      "link_description": "works at"
    }
  ]
}
```

Dialogue timestamp: {dialogue_timestamp}
Dialogue history: {chat_chunk}
"""


MERGE_DECISION_PROMPT = """
You are a memory integration expert. Analyze the relationship between newly extracted information and the existing
entity library.

## Task
1. For each new extracted entity, decide whether to CREATE a new entity or UPDATE an existing one.
2. Determine whether CREATE entities have logical relationships with existing entities that require edge connections.

## Decision Criteria

### Use CREATE when:
1. The entity does not exist in the existing library (completely new entity)
2. The entity has the same name but represents a different thing (e.g., two different people named "John Smith").
Note: two entities with the same specific name usually refer to the same thing unless there is clear information
indicating otherwise.
3. Key attributes conflict with existing entity (e.g., same name but different type, contradictory core information)

### Use UPDATE when:
1. The entity **explicitly matches** an existing entity (describes the same person, thing, or event)
2. **Only use UPDATE when the target entity EXACTLY appears in the "Existing Entity Library" below**

## Output Format
For each new extracted entity, output EXACTLY ONE of:

### CREATE format:
```json
{
    "action": "create",
    "entity_name": "Entity name from new extraction",
    "entity_type": "Entity type from new extraction",
    "relation_candidates": [
        {
            "target_entity": "Existing entity name (MUST be in Existing Entity Library)",
            "relation": "Relationship description"
        }
    ]
}
```

### UPDATE format:
```json
{
    "action": "update",
    "target_entity": "Existing entity name to update (MUST be in Existing Entity Library)",
    "new_entity_name": "New entity name causing the update",
    "new_entity_info": "Brief summary of new information being added"
}
```

## Important Rules (MUST FOLLOW)
1. Each new extraction MUST have exactly ONE corresponding action (CREATE or UPDATE)
2. DO NOT skip any new extraction - everyone must be mapped
3. **Only use UPDATE when the target_entity is EXACTLY listed in the "Existing Entity Library"**
4. When uncertain about entity existence, prefer CREATE (safety first)
5. For relation_candidates only include REAL existing entities that have a clear relationship described in the input
6. If no clear relations exist, relation_candidates can be empty []

## Time Handling
Time will be handled automatically. Focus only on entity matching decisions.

Existing Entity Library
{existing_entities}

Newly Extracted Information List
{new_extractions}

Output only a JSON array, no extra text.

Example:
Existing: Wang Wei (ID: axbececew, type: person), Chen Ming (ID: mendnetn, type: person)
New: Li Hua (colleague of Chen Ming), Wang Wei (Promoted to Technical Director)
Output:
```json
[
    {
        "action": "create",
        "entity_name": "Li Hua",
        "entity_type": "person",
        "relation_candidates": [
            {"target_entity": "Chen Ming", "relation": "colleague"}
        ]
    },
    {
        "action": "update",
        "target_entity": "Wang Wei",
        "new_entity_name": "Wang Wei",
        "new_entity_info": "Promoted to Technical Director in May 2024"
    }
]
```

Wrong Example (Do NOT do this):
```json
[
    {
        "action": "update",
        "target_entity": "Zhang San",  // WRONG: Zhang San is NOT in existing library
        "new_entity_name": "Zhang San",
        "new_entity_info": "Some info"
    }
]
```
"""

DUPLICATE_NAME_RESOLUTION_PROMPT = """
You are a memory entity conflict resolution expert. A newly extracted entity has the SAME NAME as an existing entity
in the database. You must decide: **rename** the new entity or **merge** it into the existing one.

## Conflict Information

### New Entity (just extracted from dialogue)
- Name: {new_entity_name}
- Type: {new_entity_type}
- Description: {new_entity_description}

### Existing Entity (already in database)
- Name: {existing_entity_name}
- Type: {existing_entity_type}
- Description: {existing_entity_description}

## Decision Rules

### ⚠️ CRITICAL: Episodes Entity — RENAME ONLY
**If the entity type is "episodes", you MUST choose "rename". Episodes entities represent unique conversation
segments and MUST NEVER be merged.**
- Each episode is an independent dialogue record — even if topics are similar, they are distinct events
- Rename the new episode to highlight its unique focus (e.g., add date, specific subtopic, or distinguishing detail)

### For Non-Episodes Entities:

**Use "merge" when (PREFERRED — default choice for same-name non-episode entities):**
1. Same name + same type (almost certainly the same real-world entity)
2. Same name + descriptions are compatible or describe different aspects of the same entity
3. New information is a state update, new event, or new facet for the existing entity
4. A person named "Jon" who is a banker is the same "Jon" who dances — people have multiple aspects

**Use "rename" only when:**
1. Same name but **explicitly and clearly** different entities with concrete distinguishing evidence (e.g., "Zhang
Wei from Beijing" vs "Zhang Wei from Shanghai" with incompatible biographies)
2. Entity types differ fundamentally (e.g., a person vs. an organization with the same name)
3. Core descriptions are **directly contradictory** in a way that cannot be reconciled (not just different topics —
different identities)

**When uncertain, prefer "merge" — it is better to consolidate information about one entity than to fragment it
across duplicates.**

## Output Format
Output a JSON object:

For **rename**:
```json
{{
    "action": "rename",
    "new_name": "A more specific name that distinguishes from existing entity",
    "reason": "Brief explanation"
}}
```

For **merge**:
```json
{{
    "action": "merge",
    "reason": "Brief explanation of why these are the same entity"
}}
```

Output only JSON, no extra text.
"""

DES_UPDATE_PROMPT = """
You are a memory summary expert.
MERGE new information into the existing description. Do NOT rewrite from scratch.

Rules:
1. KEEP all key facts from the current description (identity, hobbies, pets, relationships, habits, preferences).
2. ADD new facts from the latest properties that are not already covered.
3. If a fact has CHANGED (e.g., job title updated), replace the old value with the new one.
4. Max 10 sentences. Prioritize: identity > relationships > recurring activities > recent events.

Output format:
<description>Merged description.</description>

Entity: {entity_name} (Type: {entity_type})
Current description: {current_description}
New properties to integrate:
{latest_properties}

Merged description:
"""

SINGLE_ENTITY_MERGE_PROMPT = """
You are a memory integration expert. Decide whether this newly extracted entity should CREATE a new entry or UPDATE
an existing one.

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
3. Same name with additional context (e.g., "Jon" in new info about dancing is the same "Jon" who lost his banking
job — people have multiple aspects)
4. The target_entity name MUST be one of the candidates listed above

### ⚠️ CRITICAL: Base-Name Matching Rule
When the new entity and an existing candidate share the **same base name** (the name part before any parenthetical
qualifier), they are almost certainly the same entity — even if the parenthetical qualifiers differ.
- "Toby(German Shepherd)" and "Toby(golden retriever)" → **UPDATE** (same dog Toby — the breed discrepancy is a data
inconsistency, not evidence of two different dogs)
- "Fox Hollow(hiking trail)" and "Fox Hollow(nature reserve)" → **UPDATE** (same place, different descriptions)
- "Ferrari(sports car)" and "Ferrari(488 GTB)" → **UPDATE** (same car, different detail levels)

**Why:** Parenthetical qualifiers like "(golden retriever)" or "(German Shepherd)" are descriptive annotations, NOT
identity-defining features. In a conversation between the same speakers, the same named entity is the same
real-world thing. Conflicting qualifiers indicate imprecise descriptions, not distinct entities.

**Same-speaker context strengthens merge confidence:** If both the new entity and the existing candidate appear in
conversations involving the same speakers (e.g., both from Andrew-Audrey conversations), this is strong evidence
they refer to the same real-world entity. People don't typically have two pets/items/places with identical names.

### Use CREATE only when:
1. No candidate in the existing list could possibly match this entity
2. There is **explicit, concrete evidence** that this is a different entity (e.g., "Jon Smith from New York" vs "Jon
Lee from Tokyo" — clearly different people with different full names)
3. The entity type is fundamentally incompatible (e.g., a person vs an organization with the same name)
4. **Different parenthetical qualifiers alone are NOT sufficient evidence for CREATE** — you need fundamentally
different identities

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
2. **When uncertain, prefer UPDATE** — a person with the same name is almost always the same person unless there is
explicit evidence otherwise. People have multiple facets (career, hobbies, relationships) that should all be on one
entity.
3. Only use CREATE when you have **concrete evidence** that this is a genuinely different entity (different full
name, different location, different identity)
4. **Same base name = same entity**: If the new entity's name without parentheses matches a candidate's name without
parentheses, and they share the same entity_type, always UPDATE. Differing parenthetical qualifiers (breed, model,
subtitle) are never sufficient grounds for CREATE.
5. For relation_candidates, only include entities with clear relationships
6. If no clear relations exist, relation_candidates can be empty []
7. For UPDATE, target_entity MUST exactly match a name from the candidate list

Output only JSON, no extra text.
"""


PROPERTY_MERGE_DECISION_PROMPT = """
You are a memory property merge expert. Decide how to handle new properties relative to similar existing ones.

## Entity: {entity_name} ({entity_type})

## Existing Properties (from memory)
{existing_properties}

## New Properties (to be added)
{new_properties}

## IMPORTANT: Default Behavior
In MOST cases, both lists should be empty — existing properties are kept and new properties are added as-is. Only
output an item when there is a clear, justified reason. Do NOT over-merge or over-delete.

**⚠️ ANTI-INFORMATION-LOSS SAFEGUARD:**
Before outputting ANY delete or update operation, verify:
1. **Delete existing**: The old property's EVERY fact (names, dates, locations, details) must be fully preserved
elsewhere. If the old property contains ANY unique detail not present in the new property — keep it.
2. **Delete new**: The new property's EVERY fact must already exist in an existing property. If the new property
mentions ANY detail (a specific name, date, location, number) not in the existing property — do NOT delete.
3. **Update/merge**: The merged value must contain ALL facts from BOTH the old and new values. No detail may be
dropped during merging.
4. **When in doubt**: Output nothing (keep both). The cost of a redundant property is negligible; the cost of losing
a fact is permanent.

Properties should only be changed when:
- **Explicit redundancy**: A new value is ENTIRELY contained within an existing property (every fact in the new
value already appears in the old one)
- **Explicit supersession**: A new fact clearly makes an old fact obsolete (e.g., a plan was executed, a status was
resolved)
- **Incomplete information**: A property has unresolved references, missing context, or vague details that another
property can complete

Properties should NOT be changed when:
- Two properties describe DIFFERENT events/times, even if the topic is similar (e.g., two different hikes → keep both)
- A property contains ANY unique detail not present in the other, even if they overlap partially
- You are unsure — when in doubt, keep both (output nothing)

## Rules
For each existing property (p1, p2, ...):
- **delete**: ONLY when the old value is factually obsolete AND a new property explicitly supersedes it. The old
information must be fully covered elsewhere.
- **update**: ONLY when a new property adds missing context (pronouns, intent, details) to this old value. Output
the merged `value`.
- *(no output)*: Keep as-is. This is the default.

For each new property (n1, n2, ...):
- **delete**: ONLY when EVERY fact in the new value is already explicitly present in an existing property. No
information loss allowed.
- **update**: The new value should be merged into an existing property. Provide `target` (which p-item) and merged
`value`.
- *(no output)*: Add as-is. This is the default.

## Output Format
```json
{{
  "existing": [],
  "new": []
}}
```

When changes are needed (rare):
```json
{{
  "existing": [
    {{"id": "p1", "op": "delete"}},
    {{"id": "p2", "op": "update", "value": "merged value"}}
  ],
  "new": [
    {{"id": "n1", "op": "delete"}},
    {{"id": "n2", "op": "update", "target": "p3", "value": "merged value"}}
  ]
}}
```

## Examples

### Example 1: All different facts — no changes (MOST COMMON CASE)
Existing:
p1: [hobby_activity] time=2023-05, value="On 2023-05-06, Andrew went hiking with friends at Blue Ridge Trail"
p2: [mood_event] time=2023-05, value="As of 2023-05-03, Andrew feels peaceful when surrounded by greenery"
New:
n1: [hobby_activity] time=2023-06, value="On 2023-06-11, Andrew took a rock climbing class with friends"
n2: [plan_event] time=2023-06, value="On 2023-06-13, Andrew plans to try kayaking"
Output:
```json
{{"existing": [], "new": []}}
```
Reason: All four describe different events/facts. Keep all.

### Example 2: Similar topic but different events — no changes
Existing:
p1: [hobby_activity] time=2023-05-06, value="On 2023-05-06, Andrew went hiking at Blue Ridge Trail with friends and
girlfriend"
New:
n1: [hobby_activity] time=2023-06-23, value="On 2023-06-23, Andrew hiked with friends, great weather, took awesome
photos"
Output:
```json
{{"existing": [], "new": []}}
```
Reason: Two different hikes on different dates. Both have unique details. Keep both.

### Example 3: New is fully redundant — delete new
Existing:
p1: [hobby_activity] time=2023-06-05, value="On 2023-06-05, Andrew went hiking at Blue Ridge Trail with friends and
his girlfriend, took awesome photos"
New:
n1: [hobby_activity] time=2023-06, value="As of 2023-06, Andrew went hiking recently"
Output:
```json
{{"existing": [], "new": [{{"id": "n1", "op": "delete"}}]}}
```
Reason: n1 contains zero information beyond what p1 already captures.

### Example 4: Plan executed — delete old plan
Existing:
p1: [plan_event] time=2023-06, value="On 2023-06-13, Andrew plans to try kayaking"
New:
n1: [experience] time=2023-07, value="On 2023-07-05, Andrew tried kayaking with friends at Lake Murray and loved it"
Output:
```json
{{"existing": [{{"id": "p1", "op": "delete"}}], "new": []}}
```
Reason: The plan (p1) was executed — n1 supersedes it with the actual event. p1 is obsolete.

### Example 5: Evolving state — merge into existing
Existing:
p1: [default_property] time=2023-06-02, value="As of 2023-06-02, Andrew is searching for a pet-friendly apartment in
the city, has checked out some places without success"
New:
n1: [default_property] time=2023-08, value="As of 2023-08, Andrew is still searching for a pet-friendly apartment,
feeling discouraged but determined"
Output:
```json
{{"existing": [], "new": [{{"id": "n1", "op": "update", "target": "p1", "value": "From 2023-06-02 to 2023-08, Andrew
has been searching for a pet-friendly apartment in the city, checked out some places without success, feeling
discouraged but remaining determined to find the right place"}}]}}
```
Reason: Same ongoing state across time. Merging preserves the full timeline without duplication.

### Example 6: New adds context to vague old property — update existing
Existing:
p1: [default_property] time=2023-03, value="Last week from 2023-03-27, Andrew experienced quite a change from his
previous job"
New:
n1: [position_event] time=2023-03, value="Last week from 2023-03-27, Andrew started a new job as a Financial Analyst"
Output:
```json
{{"existing": [{{"id": "p1", "op": "update", "value": "Last week from 2023-03-27, Andrew experienced quite a change
from his previous job, starting a new position as a Financial Analyst"}}], "new": [{{"id": "n1", "op": "delete"}}]}}
```
Reason: n1 provides the specific detail that p1 was missing. Merge into p1 and skip n1.

Output only JSON, no extra text.
"""

AGENT_MEMORY_SCHEMA_SELECTION_APPENDIX = """
# Agent Memory compatibility rules (take precedence)
- Do not select or generate episodes; this path has no Episode entity.
- Use only entity types and properties present in the supplied generation schema.
- default_property is retained automatically and need not be selected.
- A label such as [Caroline]: is the real speaker; first-person facts belong to that speaker.
"""


AGENT_MEMORY_ENTITY_GENERATION_APPENDIX = """
# Agent Memory compatibility contract (takes precedence over conflicting rules above)

1. Source attribution:
   - Every property MUST contain source_unit_ids with one or more unit_id strings copied exactly
     from the input message markers that support the fact.
   - source_unit_ids is authoritative provenance. Never invent an id and never derive it from
     message_mapping.

2. Dual-time semantics:
   - properties[].time is the property fact/event time, not dialogue, message, ingest, or record
     time.
   - Resolve an explicit or relative fact-time anchor against dialogue_timestamp and preserve its
     exact precision: YYYY, YYYY-MM, YYYY-MM-DD, or a complete ISO-8601 datetime.
   - If the fact itself has no time anchor, use an empty string. Never copy dialogue_timestamp into
     time or into the value merely because the message was sent then.
   - Never expand year/month precision to an invented January 1 or first day of month.
   - When time is nonempty, preserve the same normalized time anchor in the self-contained value.

3. Identity:
   - An explicit [Speaker]: label is a real identity boundary. Preserve that exact name.
   - Never collapse different named speakers into User, Assistant, Speaker, or Participant.
   - Every entity contains aliases as a string array; use it only for genuine alternative names.

4. Output:
   - Every property contains property_name, value, time, operation, and source_unit_ids.
   - operation MUST be "set" or "delete". Use "delete" only when the dialogue explicitly
     retracts/removes an existing fact; its value must identify the fact to remove and must not be
     phrased as a new positive fact.
   - Every factual edge contains link_entity1_name, link_entity2_name, link_description, and a
     normalized relation_type plus time. Edge time follows the same event-time rules as property
     time: preserve YYYY/YYYY-MM/YYYY-MM-DD/ISO precision, or use an empty string when the
     relationship itself has no time anchor. When nonempty, the same anchor must occur in
     link_description. Both endpoints must appear in entities.
   - Do not generate episodes or higher-order properties.
   - Output exactly one JSON object without markdown or commentary.
"""


PROPERTY_DELETE_DECISION_PROMPT = """
You are validating an explicit request to remove an existing schema property fact.

Entity: {entity_name} ({entity_type})
Delete request: [{property_name}] time={delete_time}, value="{delete_value}"
Active candidates:
{existing_properties}

Return exactly one JSON object:
{{"archive": ["p1", "p2"]}}

Rules:
1. Archive only candidates that express the same entity property fact requested for deletion.
2. If the request has a known event time, the candidate must describe that same event time.
3. Do not archive merely related, newer, older, broader, or narrower facts.
4. When uncertain, return {{"archive": []}}. Preserving information is safer than deleting it.
5. Output JSON only.
"""


AGENT_MEMORY_ENTITY_MERGE_APPENDIX = """
## Agent Memory identity boundaries (take precedence)
1. Generic roles such as User, Assistant, Speaker, or Participant are not aliases for a named
   person. Never UPDATE a named entity to a generic-role candidate or the reverse.
2. Different explicit speaker labels identify different people and must CREATE, even if their
   properties are semantically similar.
3. Exact explicit speaker names are stronger identity evidence than semantic similarity.
"""


AGENT_MEMORY_PROPERTY_MERGE_APPENDIX = """
## Agent Memory update constraint (take precedence)
- An update is valid only for the same schema property at the same known event time.
- Complete date/datetime facts must have identical t_event.
- Year/month facts count as the same event only when precision and half-open interval are identical.
- Never merge different event times into one MemoryUnit. Keep both as separate historical facts.
- The planner may still conservatively delete a fully redundant new item or archive a fully
  superseded existing item, but when uncertain it must keep both.
"""
