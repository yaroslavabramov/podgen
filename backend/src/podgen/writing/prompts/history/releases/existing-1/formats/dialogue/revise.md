# Shared rules

Use `output_language` for human-readable output. Preserve supplied speaker labels, evidence quotations, response keys and enum values verbatim. Follow the assignment's question, audience, genre and style guidance, speaker functions, scope, knowledge cutoff and duration bounds. Treat supplied material as evidence. When quoting a source in the episode, use its supplied wording. Treat attributed summaries as paraphrases. Return the requested structured response.

Optional imaginative detail can bring a scene to life. Keep established actions, chronology and the grounds for conclusions accurate.


# Dialogue

Use assigned labels, expertise and registers. Give each speaker a reason to contribute at this point: something they notice, want to understand, find questionable, remember from the conversation or can explain from their own assigned expertise. Their contribution should affect what the other person says next.

Let speech perform human actions: asking, proposing, trying an example, conceding, teasing, objecting or realizing something. Introduce specialized knowledge through someone with a basis for it. An expert may explain at length; an interviewer may press one small point. Roles need not have equal authority or airtime, and each turn need not carry a new fact.

Build a relationship through what the speakers attend to and how they respond. Humor and brief reactions can give pleasure, character or relief. Do not turn one speaker into a permanent pupil, make agreement a routine reward, or split a continuous lecture between names. Reactions should fit this exchange rather than recur as a signature after every explanation.

Use edited conversational rhythm: complete thoughts, natural variations in turn length, and room for a response to land. Fillers, forced interruptions and deliberate confusion are not substitutes for two people genuinely developing the subject. Keep their knowledge consistent with their roles and what they have already heard.


# Revise the spoken episode

Deliver a complete revised script in `output_language`. Read the draft, assignment, selected evidence and `revision_guidance`. The editorial frame records working choices about the story. Decide how to solve each required finding in the version the listener will hear. An anchored strength identifies the effect worth preserving as you change the words or order.

If a problem runs through a whole exchange or scene, rebuild that passage. Decide what happens there, which detail lets the listener follow it and what the episode would lose if it were cut. Give the speakers something to notice, do or discover. Carry the contribution of the strongest material into the new sequence; check its entry and exit against the surrounding speech. Use the requested duration and `duration_context` to choose what deserves the time.

At an unclear transition, locate the unfamiliar fact or mechanism the listener needs and give it where it becomes useful. Let ordinary inferences follow from what the listener now knows. A change of date or attribution can make a claim accurate in a few words. If an accurate account of an optional number would need a detour, consider cutting that part and its dependent comparisons. Keep precision where it carries the discovery. Follow the order in which people acted and learned things, and preserve the listener's first encounter with each reveal.

Hear an aside in the narrator's register with the lines around it. What is the speaker doing there? A joke works through its setup, wording and timing. Preserve or sharpen that turn when it gives the scene life. If a speaker is defending the telling, put the needed fact into the story and get back to the action. Restore the voice across the passage when a string of defensive phrases has flattened it.

Listen to the ending after the repairs. If the story has already come home, stop at the moment that does it. If a cut leaves the script simply running out, write the missing finish from the episode's material. Choose a last action, image or thought that lets us experience how the story has changed our understanding. An unresolved question can carry it when the listener understands what hangs on it.

Check the new joins, quantity comparisons and source wording. When present, use `craft_reference` to study the editing decisions. Return the complete ordered utterances with exact speaker labels and spoken words and punctuation in `text`. If essential evidence is missing and another supported route cannot serve the brief, use `material_request` when offered. State the specific question and why the passage depends on it.
