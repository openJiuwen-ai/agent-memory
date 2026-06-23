from string import Template


validation_prompt = """
 Your task is to label an answer to a question as ’CORRECT’ or ’WRONG’. You will be given the following data:
        (1) a question (posed by one user to another user), 
        (2) a ’gold’ (ground truth) answer, 
        (3) a generated answer
    which you will score as CORRECT/WRONG.

    The point of the question is to ask about something one user should know about the other user based on their prior conversations.
    The gold answer will usually be a concise and short answer that includes the referenced topic, for example:
    Question: Do you remember what I got the last time I went to Hawaii?
    Gold answer: A shell necklace
    The generated answer might be much longer, but you should be generous with your grading - as long as it touches on the same topic as the gold answer, it should be counted as CORRECT. 

    For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like "last Tuesday" or "next month"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., "May 7th" vs "7 May"), consider it CORRECT if it's the same date.

    Now it’s time for the real question:
    Question: {question}
    Gold answer: {gold_answer}
    Generated answer: {response}

    First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG. 
    Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

    Just return the label CORRECT or WRONG in a json format with the key as "label".
"""

ANSWER_PROMPT = Template("""
# Role
You are an intelligent assistant capable of leveraging prior context to answer questions accurately and coherently.

Please follow these guidelines:

- If the Memory contains relevant information that directly addresses the Question, use it as the primary basis for your answer.
- If the Memory is empty, irrelevant, or insufficient, answer using your general knowledge—but do not fabricate details or pretend the memory contains information it doesn’t.
- If the memory is partial or ambiguous, acknowledge that clearly and supplement with reasonable inference or clarification when appropriate.
- Keep your response concise, natural, and **directly** responsive to the question.

# Notice
- Every memory should has its own conversation time, carefully understanding the conversation information and analysis 
the event time based on the conversation time.
- You can just answer the question **directly**, no need to explain how you get the answer.

# Question: 

$question

# Memory Info: 

conversation content: $memory

Now, answer the question based on the above instructions.
""")