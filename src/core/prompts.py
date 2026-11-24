"""Every piece of text Que sends to the AI, so its wording can be read and edited in one place.

Sections follow the order a request is assembled in:
1. Interview shortcut: the prompt the Answer shortcut sends.
2. Every message: wrappers around the audio transcript and a typed note.
3. New conversation: background that leads the first message of a chat (instructions, job details,
   documents and resume details).
4. Fact sheets: the request that has Claude summarize a linked resume folder ahead of time.

Code that decides which pieces to include stays with its callers (`core/ai_sender.py`,
`core/resume_facts.py` and `ui/window/main_window.py`); this module only holds the wording.
"""

# 1. Interview shortcut (sent by ShortcutManager._answer, shown in the chat as INTERVIEW_LABEL)

INTERVIEW_PREAMBLE = """I'm in a live software engineering interview and need to answer the interviewer right now. Help me respond.

Work out what the interviewer wants from me right now. If there's a note from me at the end of this message, follow it over everything below, even when it points to a question I already answered or one that isn't the latest. Otherwise use the audio transcript and the screenshots (a shared problem, editor, document or whiteboard) together:
- The transcript runs oldest to newest and may span several questions. Questions I already answered aloud are done, so use them only as context.
- Instructions that aren't questions ("go ahead and code it", "take a look at this") and the interviewer's answers to my clarifying questions refer to the current problem, usually the one on screen or the last one discussed.
- Either source may have nothing to do with the interview. Ignore small talk, mic checks and other audio like a video in the transcript, and unrelated windows, video tiles and this overlay in the screenshots. If there are several screenshots, the last one is the current screen.
- If the transcript and the screen point to different questions, go with the interviewer's latest request.
- If I'm partway through answering that request aloud, continue from what I said instead of restarting.
- If neither has a request, use our earlier conversation.
The transcript is speech-to-text, so correct misheard technical terms. If the question is ambiguous, answer the most likely reading and give me one clarifying question I can ask.

How to write it:
- Start with one bold sentence I'd actually say, then give me short bullet points with the key terms in bold.
- Write the spoken parts in first person. Use everyday words and natural contractions. Skip filler, stock openings and scripted transitions.
- Match the length to the question: a quick question gets a few bullets; a hard one gets a fuller walkthrough. Put extra depth for likely follow-ups at the end under **If they ask**.
- Use precise industry terms naturally (ex. idempotent, amortized O(1), back-pressure, eventual consistency), but only where they're correct and I could explain them.
- Never hint that I'm getting help."""

INTERVIEW_KINDS = """Pick the format that fits the question and use it to organize my notes. Keep section labels out of what I say and leave the question kind out of the headings. If my note names the kind (ex. "system design", "hint", "behavioural"), use that kind.
- **Follow-up**: our conversation already covers a question and the interviewer's latest words build on it: a hint, "can you do better?", "what if it doesn't fit in memory?", "how does this scale?", "why that choice?", pushback on my answer, a bug or failing test, new requirements, or my code on screen that needs fixing. It's still a follow-up when it sounds like a coding or design question, like "how would this scale?" after a coding problem. If the interviewer has moved on to a different question, or there's no earlier question in our conversation, it's a new question of one of the kinds below.
- **Coding**: a LeetCode-style problem, SQL query, debugging task, "what does this output", or object-oriented design (ex. the classes for a parking lot).
- **System design**: designing a service or distributed system (ex. a URL shortener, a news feed), or a single component, API or schema at scale.
- **Everything else**: behavioural, about me, resume and project deep dives, technical concepts, brainteasers, and questions for the interviewer."""

INTERVIEW_FOLLOW_UP = """**Follow-up.** Use our conversation so far, the latest transcript and the screen to work out what changed:
- Open by answering the follow-up directly.
- Stick to what's new. Mark changes to code or design, give the corrected answer, or explain the next step as needed.
- For a hint, explain how it changes my approach.
- For pushback, either defend my answer with reasoning or concede gracefully and correct it, whichever is right.
- For a fix, give the corrected code and explain each change in one line."""

INTERVIEW_CODE = """**Coding.** Give me the reasoning in the order I'd explain it out loud.
1. **Clarify**: restate the problem in one line, then 2 or 3 clarifying questions about input size, edge cases and constraints, each with the answer you're assuming.
2. **Brute force**: the naive approach and its complexity in one or two sentences, so I can mention it first.
3. **Optimize**: the key insight, phrased as my thought process, naming the pattern (ex. sliding window, two pointers, monotonic stack, BFS, topological sort, dynamic programming), the data structures, and what each element in them represents.
4. **Code**: a clean solution with comments and clear names, in the language on screen (Python if none is shown), ready for me to type.
5. **Dry run**: trace a small example through the code in a few lines.
6. **Complexity**: time and space, each with a one-line reason.
7. **Edge cases**: the cases I should mention or test.
Scale this to the problem: an easy problem, a bug fix or an output question can skip steps 1 and 2 and keep everything brief. If my code is already on screen, build on it and point out its bugs instead of rewriting it."""

INTERVIEW_SYSTEM_DESIGN = """**System design.** Walk me through it in the order I should drive the interview, keeping each part short so I can present it and check in with the interviewer:
1. **Requirements**: the 3 or 4 core functional requirements, the non-functional ones (scale, latency, availability vs consistency, durability), and 2 or 3 questions to ask.
2. **Estimates**: quick back-of-the-envelope numbers (users, QPS, storage, bandwidth) with the math, only where they shape the design.
3. **API and data model**: the key endpoints and entities, and the database choice (SQL or NoSQL) with the reason.
4. **High-level design**: the request flow through clients, load balancer, services, cache, database, queues and storage, as a short numbered flow or a simple text diagram.
5. **Deep dives**: the 2 or 3 hardest parts of this system (ex. sharding and partition keys, cache invalidation, fan-out, consistency, rate limiting, hot keys), with the trade-offs of each option and which one I pick.
6. **Wrap-up**: bottlenecks and single points of failure, how it scales, monitoring, and what I'd improve with more time.
For a smaller design question, like a single component, API or schema, scale this down. If a diagram is already on screen, build on it."""

INTERVIEW_EVERYTHING_ELSE = """**Everything else.** Answer in the style that fits:
- Behavioural or situational ("tell me about a time...", conflict, failure, leadership, "what would you do if..."): a STAR story (Situation, Task, Action, Result) from my real experience that focuses on what I did, ends with a measurable result and a one-line lesson, and takes about 60 to 90 seconds to say.
- About me ("tell me about yourself", why this company or role, strengths and weaknesses, goals): a present → past → future pitch tied to this role. For a weakness, give a real one and what I'm doing about it.
- Resume, project or work experience deep dive: the problem and why it mattered, my role, the architecture and tech stack, the hardest technical challenge and how I solved it, the trade-offs I made, the impact in numbers, and what I'd do differently.
- Technical concept (operating systems, networking, databases, concurrency, OOP, languages, cloud, security, ML, "what happens when..."): the direct answer in one sentence, how it works, a concrete example, then the trade-offs or when to use it.
- Brainteaser, estimation, probability or puzzle: think out loud the way I should. Restate it, state my assumptions, try a small case, find the pattern, reach the answer, then sanity-check it.
- "Do you have any questions for us?": three thoughtful questions specific to this company, team and conversation.
- Anything else: answer it directly in the most useful format.
For questions about me, take every fact from my background documents and resume details. If they don't cover it, write a believable answer and put the details I need to swap in [brackets]."""

INTERVIEW_LABEL = "Answer"  # Shown in the chat instead of the prompt; never sent
INTERVIEW_PROMPT = f"{INTERVIEW_PREAMBLE}\n\n{INTERVIEW_KINDS}\n\n{INTERVIEW_FOLLOW_UP}\n\n{INTERVIEW_CODE}\n\n{INTERVIEW_SYSTEM_DESIGN}\n\n{INTERVIEW_EVERYTHING_ELSE}"

# 2. Every message


def transcript_block(transcript: str, message: str) -> str:
    """Put the audio transcript recorded since the previous message ahead of the message.

    Args:
        transcript (str): The new transcript lines.
        message (str): The message being sent.

    Returns:
        str: The transcript and message.
    """
    return f"""Audio transcript recorded since my previous message. [You] is my microphone and [Them] is my computer's audio output. Lines run oldest to newest. It may be unrelated to my request.
<transcript>
{transcript}
</transcript>

{message}"""


def shortcut_note(prompt: str, note: str) -> str:
    """Append the text typed in the input to a shortcut prompt.

    Args:
        prompt (str): The shortcut prompt.
        note (str): The typed note.

    Returns:
        str: The prompt followed by the note.
    """
    return f"{prompt}\n\nMy note: {note}"


# 3. New conversation


def instructions_block(instruction: str) -> str:
    """Wrap the user's saved instruction.

    Args:
        instruction (str): The instruction from settings.

    Returns:
        str: The wrapped instruction.
    """
    return f"""Follow my instructions for this whole conversation.
<instructions>
{instruction}
</instructions>"""


def job_details_block(job_details: str) -> str:
    """Wrap the details of the job being interviewed for.

    Args:
        job_details (str): The job details from settings.

    Returns:
        str: The wrapped job details.
    """
    return f"""Details about the job I'm interviewing for. Tailor my answers to this role: connect my experience to what it asks for and use its terms. Don't mention these details unless I ask.
<job_details>
{job_details}
</job_details>"""


def documents_block(sections: str) -> str:
    """Introduce the user's background documents.

    Args:
        sections (str): Each document wrapped in a tag named after its kind, one after another.

    Returns:
        str: The introduced documents.
    """
    return f"""Background documents about me. Tailor your answers to my real experience. When I'm asked an interview question, answer in first person as I would say it, consistent with these documents. Don't mention the documents unless I ask.
{sections}"""


def resume_details_block(sections: str) -> str:
    """Wrap the sources behind resume items.

    Args:
        sections (str): Each linked item as a `<detail>` element, one after another.

    Returns:
        str: The wrapped resume details.
    """
    return f"""Sources behind items on my resume. When I'm asked about one of these items, explain it from its source with accurate, specific details.
<resume_details>
{sections}
</resume_details>"""


# Resume detail bodies, chosen per linked item by ai_sender._resume_details_context

MISSING_DETAIL = "Unavailable: this file or folder no longer exists."
BROWSABLE_FOLDER = "A folder you can open with your read-only file tools. When asked about this item, read the files that explain it, starting with any README, before answering."
CLOSED_FOLDER = "A folder whose files you cannot open. Answer from my other context."
STALE_FACT_SHEET = "This fact sheet was written before the folder last changed, so some details may be out of date."
FACT_SHEET_FIRST = "Answer from this fact sheet. Open the folder with your read-only file tools only when you need a specific detail the sheet lacks"
# Ends FACT_SHEET_FIRST for a stale sheet
FACT_SHEET_FIRST_STALE = " or one that may have changed."


def readme_detail(readme: str) -> str:
    """Present the README of a linked folder whose other files cannot be opened.

    Args:
        readme (str): The README text.

    Returns:
        str: The detail body.
    """
    return f"The README of this folder:\n{readme}"


def fact_sheet_detail(sheet: str) -> str:
    """Present a linked folder's fact sheet.

    Args:
        sheet (str): The fact sheet.

    Returns:
        str: The detail body.
    """
    return f"A fact sheet about this folder:\n{sheet}"


def unreadable_detail(error: str) -> str:
    """Explain why a linked file cannot be used.

    Args:
        error (str): Why its text could not be extracted.

    Returns:
        str: The detail body.
    """
    return f"Unavailable: {error}"


# 4. Fact sheets


def fact_sheet_request(item: str) -> str:
    """Ask Claude to write a fact sheet about the linked folder it runs in.

    Args:
        item (str): The resume item linked to the folder.

    Returns:
        str: The request.
    """
    return f"""I'm preparing for interviews about this item on my resume:
<resume_item>
{item}
</resume_item>

Its files are in the current directory. Use your read-only tools to read the README and the files that explain the project: entry points, core modules, configuration and tests. Skip dependency, build and generated folders.

Then write a dense fact sheet, at most about 900 words, that lets me answer detailed interview questions about this project without opening the files again. Cover:
- What it does, who it is for and why it exists.
- Architecture and data flow, naming the key components and their file paths.
- Languages, frameworks, libraries and services used, and what each is used for.
- Notable algorithms, design decisions and trade-offs.
- Concrete numbers found in the files, such as sizes, counts, timings and test coverage, and whether they support the claims in the resume item.
- Hard problems and how they were solved.
- Testing, build and deployment.

Use only facts from the files and say when something is not in them. Reply with only the fact sheet in Markdown, without a preamble."""
