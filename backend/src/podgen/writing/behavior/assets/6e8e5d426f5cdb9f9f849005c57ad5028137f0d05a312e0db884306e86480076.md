# Shared rules

Use `output_language` for human-readable output. Preserve supplied speaker labels, evidence quotations, response keys and enum values verbatim. Follow the assignment's question, audience, genre and style guidance, speaker functions, scope, knowledge cutoff and duration bounds. Treat supplied material as evidence. When quoting a source in the episode, use its supplied wording. Treat attributed summaries as paraphrases. Return the requested structured response.

Optional imaginative detail can bring a scene to life. Keep established actions, chronology and the grounds for conclusions accurate.


# Dialogue

Use assigned labels, expertise and registers. Give each speaker a reason to contribute at this point: something they notice, want to understand, find questionable, remember from the conversation or can explain from their own assigned expertise. Their contribution should affect what the other person says next.

Let speech perform human actions: asking, proposing, trying an example, conceding, teasing, objecting or realizing something. Introduce specialized knowledge through someone with a basis for it. An expert may explain at length; an interviewer may press one small point. Roles need not have equal authority or airtime, and each turn need not carry a new fact.

Build a relationship through what the speakers attend to and how they respond. Humor and brief reactions can give pleasure, character or relief. Do not turn one speaker into a permanent pupil, make agreement a routine reward, or split a continuous lecture between names. Reactions should fit this exchange rather than recur as a signature after every explanation.

Use edited conversational rhythm: complete thoughts, natural variations in turn length, and room for a response to land. Fillers, forced interruptions and deliberate confusion are not substitutes for two people genuinely developing the subject. Keep their knowledge consistent with their roles and what they have already heard.


# Write the episode

Write the complete spoken episode for the assigned audience, speakers and duration in `output_language`. The accepted plan chooses what to develop, what to mention in passing and what to leave in the reporting. Carry those choices into the telling, even if you move a scene. Use the full sources to make the selected moments vivid and exact in your own words. Reopen a cut when a turn in the story would otherwise be lost or misunderstood.

Open with something the listener can already picture or recognize. Before asking them to share someone's suspicion, give them the observation or working belief that made it possible. Then follow what the person does about it. In an explanation, supply the unfamiliar fact or mechanism the next step depends on. Once the listener has the pieces for an ordinary inference, let the next action, comparison or reply give them the pleasure of making it. Spend another sentence on a connection only while it is still hard to see.

Choose details for what they change in the telling. A source excerpt can give a scene a particular voice, an unexpected habit or the actual sequence of a discovery. Save the discovery for the encounter that earns it. Where several accounts or sets of figures do the same work, choose the one that lets the listener follow what matters. Keep a second when it changes the stakes or tests a different idea.

Write idiomatic speech with room for the speaker's own attitude. Let a second voice do something with the first speaker's thought. Listen to an aside with the line before it and the one after. A dry joke can turn a scene with its choice of words and timing. If the speaker is stepping aside to explain their caution, put the precise fact into the account and let the action continue.

Decide what has changed by the end. Give that change a last moment the listener can experience: something that happens, an image now seen differently, or a thought the story has made possible. If the outline runs out before the story feels finished, write the missing beat from the material. An open question can carry the ending when the listener can feel what is still at stake. Stop when that moment lands.

Check numbers, attribution, chronology and what each person could know. Use supplied source wording for direct quotes. If present, use `craft_reference` to study editorial decisions. If essential evidence is missing and another supported route cannot serve the brief, return the offered `material_request` with the precise question and its bearing on the passage. Otherwise deliver the utterances in playback order with exact speaker labels and spoken words and punctuation in `text`.
