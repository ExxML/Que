"""Prompt for the interview shortcut: the chat label and the full text sent to the model."""

PREAMBLE = """I'm in a live software engineering interview and need to answer the interviewer right now. Help me respond.

Work out what the interviewer wants from me right now. If there's a note from me at the end of this message, follow it over everything below, even when it points to a question I already answered or one that isn't the latest. Otherwise use the audio transcript and the screenshots (a shared problem, editor, document or whiteboard) together:
- The transcript runs oldest to newest and may span several questions. Questions I already answered aloud are done, so use them only as context.
- Instructions that aren't questions ("go ahead and code it", "take a look at this") and the interviewer's answers to my clarifying questions refer to the current problem, usually the one on screen or the last one discussed.
- Either source may have nothing to do with the interview. Ignore small talk, mic checks and other audio like a video in the transcript, and unrelated windows, video tiles and this overlay in the screenshots. If there are several screenshots, the last one is the current screen.
- If the transcript and the screen point to different questions, go with the interviewer's latest request.
- If I'm partway through answering that request aloud, continue from what I said instead of restarting.
- If neither has a request, use our earlier conversation.
The transcript is speech-to-text, so correct misheard technical terms. If the question is ambiguous, answer the most likely reading and give me one clarifying question I can ask.

How to write it:
- I'm reading this while I talk, so make it easy to scan: start with one bolded sentence I can say word for word, then short bullet talking points with the key terms in bold.
- Write what I say in first person, in natural spoken English with contractions. No essay prose, no filler.
- Match the length to the question: a quick question gets a few bullets; a hard one gets a fuller walkthrough. Put extra depth for likely follow-ups at the end under **If they ask**.
- Use precise industry terms naturally (ex. idempotent, amortized O(1), back-pressure, eventual consistency), but only where they're correct and I could explain them.
- Never hint that I'm getting help."""

KINDS = """First decide which kind of question it is, then answer in that kind's format below. Don't write the kind as a heading. If my note names the kind (ex. "system design", "hint", "behavioural"), use that kind.
- **Follow-up**: our conversation already covers a question and the interviewer's latest words build on it: a hint, "can you do better?", "what if it doesn't fit in memory?", "how does this scale?", "why that choice?", pushback on my answer, a bug or failing test, new requirements, or my code on screen that needs fixing. It's still a follow-up when it sounds like a coding or design question, like "how would this scale?" after a coding problem. If the interviewer has moved on to a different question, or there's no earlier question in our conversation, it's a new question of one of the kinds below.
- **Coding**: a LeetCode-style problem, SQL query, debugging task, "what does this output", or object-oriented design (ex. the classes for a parking lot).
- **System design**: designing a service or distributed system (ex. a URL shortener, a news feed), or a single component, API or schema at scale.
- **Everything else**: behavioural, about me, resume and project deep dives, technical concepts, brainteasers, and questions for the interviewer."""

FOLLOW_UP = """**Follow-up.** Use our conversation so far, the latest transcript and the screen to work out what changed:
- Start with a natural one-line response I can say (ex. "Good point, that breaks when...").
- Then answer only the new part (the changed code or design with the changes marked, a corrected answer, or the next step) without repeating what we've already covered.
- For a hint, show how it changes my approach as if I just realized it.
- For pushback, either defend my answer with reasoning or concede gracefully and correct it, whichever is right.
- For a fix, give the corrected code and explain each change in one line."""

CODE = """**Coding.** Walk me through it the way a strong candidate thinks out loud, rather than jumping straight to the answer:
1. **Clarify**: restate the problem in one line, then 2 or 3 clarifying questions about input size, edge cases and constraints, each with the answer you're assuming.
2. **Brute force**: the naive approach and its complexity in one or two sentences, so I can mention it first.
3. **Optimize**: the key insight, phrased as my thought process, naming the pattern (ex. sliding window, two pointers, monotonic stack, BFS, topological sort, dynamic programming), the data structures, and what each element in them represents.
4. **Code**: a clean solution with comments and clear names, in the language on screen (Python if none is shown), ready for me to type.
5. **Dry run**: trace a small example through the code in a few lines.
6. **Complexity**: time and space, each with a one-line reason.
7. **Edge cases**: the cases I should mention or test.
Scale this to the problem: an easy problem, a bug fix or an output question can skip steps 1 and 2 and keep everything brief. If my code is already on screen, build on it and point out its bugs instead of rewriting it."""

SYSTEM_DESIGN = """**System design.** Walk me through it in the order I should drive the interview, keeping each part short so I can present it and check in with the interviewer:
1. **Requirements**: the 3 or 4 core functional requirements, the non-functional ones (scale, latency, availability vs consistency, durability), and 2 or 3 questions to ask.
2. **Estimates**: quick back-of-the-envelope numbers (users, QPS, storage, bandwidth) with the math, only where they shape the design.
3. **API and data model**: the key endpoints and entities, and the database choice (SQL or NoSQL) with the reason.
4. **High-level design**: the request flow through clients, load balancer, services, cache, database, queues and storage, as a short numbered flow or a simple text diagram.
5. **Deep dives**: the 2 or 3 hardest parts of this system (ex. sharding and partition keys, cache invalidation, fan-out, consistency, rate limiting, hot keys), with the trade-offs of each option and which one I pick.
6. **Wrap-up**: bottlenecks and single points of failure, how it scales, monitoring, and what I'd improve with more time.
For a smaller design question, like a single component, API or schema, scale this down. If a diagram is already on screen, build on it."""

EVERYTHING_ELSE = """**Everything else.** Answer in the style that fits:
- Behavioural or situational ("tell me about a time...", conflict, failure, leadership, "what would you do if..."): a STAR story (Situation, Task, Action, Result) from my real experience that focuses on what I did, ends with a measurable result and a one-line lesson, and takes about 60 to 90 seconds to say.
- About me ("tell me about yourself", why this company or role, strengths and weaknesses, goals): a present → past → future pitch tied to this role. For a weakness, give a real one and what I'm doing about it.
- Resume, project or work experience deep dive: the problem and why it mattered, my role, the architecture and tech stack, the hardest technical challenge and how I solved it, the trade-offs I made, the impact in numbers, and what I'd do differently.
- Technical concept (operating systems, networking, databases, concurrency, OOP, languages, cloud, security, ML, "what happens when..."): the direct answer in one sentence, how it works, a concrete example, then the trade-offs or when to use it.
- Brainteaser, estimation, probability or puzzle: think out loud the way I should. Restate it, state my assumptions, try a small case, find the pattern, reach the answer, then sanity-check it.
- "Do you have any questions for us?": three thoughtful questions specific to this company, team and conversation.
- Anything else: answer it directly in the most useful format.
For questions about me, take every fact from my background documents and resume details. If they don't cover it, write a believable answer and put the details I need to swap in [brackets]."""

LABEL = "Answer"
PROMPT = f"{PREAMBLE}\n\n{KINDS}\n\n{FOLLOW_UP}\n\n{CODE}\n\n{SYSTEM_DESIGN}\n\n{EVERYTHING_ELSE}"
