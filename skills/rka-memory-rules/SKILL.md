---
name: rka-memory-rules
description: What to do when the user wants rka-memory to remember something, such as "remember that...", "from now on...", "always/never...", "以后...", "记住...", or when the user corrects you and wants the correction to stick in later sessions.
---

# Helping the user teach rka-memory a rule

You cannot write to rka-memory, and you should not try: lessons in your own
words are the channel that carried false memories in earlier tests. The user
teaches it directly, in their own words. Your job is to tell them how, and
then to get out of the way.

## How a rule the user states becomes memory

When a session ends, rka-memory reads the user's messages for standing rules.
A statement counts when all of these hold:

- It is in one of the user's own messages, as they typed it.
- It is between 12 and 400 characters.
- Within 200 characters of the rule, in the same message, there is a phrase
  that makes it standing: "from now on", "going forward", "always",
  "never", "every time", "whenever", "by default", "remember", "as a rule",
  "以后", "今后", "从现在起", "每次", "总是", "始终", "永远", "记住", "默认", and
  similar.
- Its wording did not come from you. If the same words appear earlier in
  anything you wrote in this project (a reply, a file you changed), the
  statement does not count. This keeps your text from posing as the user's.

Such a rule is delivered from the next session on, quoted as
`The user said: "..."`.

## What to tell the user

1. If what they said already meets the rule above, tell them it will be
   recorded in their words at the end of the session. Do not repeat it back
   in different words.
2. If it does not (no standing phrase, or it only says "yes, do that"),
   ask them to state it once in their own words with a standing phrase,
   for example: "From now on, run the linter before every commit." Give that
   as a shape, not as the sentence to copy: if they copy your sentence, its
   words come from you and it will not count.
3. If they want to confirm a lesson that is already a note, they type
   `rkm confirm <record-id>` as the whole prompt. That always counts.
4. If they want to change a note: `rkm qualify <id> <condition>`,
   `rkm correct <id> <new wording>`, or `rkm reject <id> <reason>`.

In Antigravity, sessions are not recorded, so a stated rule is not picked up.
The user can state it in Claude Code or Codex, or confirm or correct
existing notes in a terminal with `rka-memory rkm ...`.

## Do not

- Do not say "I'll remember that" or "noted for next time".
- Do not write the rule into a file, a notes document, or a memory system of
  your own as a substitute, unless the user asks for that file.
- Do not draft the rule for the user to paste.
