# Russian number alignment and export recovery

This review compared the Snow and Apollo transcripts and their saved recognition segments with the reprocessed version 4 reports. Each recording still had one ASR submission and the recorded wire hash matched the frozen manifest.

| Case | Interpretation |
| --- | --- |
| Snow's 535, 230 and 896 and Apollo's 24, 20 and 15 | Supported Russian grammatical forms matched equal whole numbers. The equivalence record retained the original tokens and positions. |
| August 31 and November 25 | The ordinal rule was limited to day 1–31 in a month context. |
| Apollo's “94 с половиной” and `94,5`, and `0,2`, `0,1` and `1,8` | Fractions and decimals remained available for review, as did disagreements between the full transcript and its segments. |
| Names, word endings and negations | Differences such as “Сноу”/“снова он” and “не”/“ни” remained literal candidates. |

Word grouping stops at turn, punctuation, newline and dash boundaries. The integer branch excludes signs, decimals, fractions, ranges and leading zeroes. The review found an independent counterexample to treating “сто ноль” as 100. Multiword forms containing “ноль” subsequently stayed literal.

Reprocessing produced **41 candidates and 27 integer equivalences for Snow**, and **19 candidates and three equivalences for Apollo**. The reviewer inspected every reported equivalence. Six transcript/segment conflicts remained on Apollo. These counts describe text alignment and require an audio check before declaring a speech defect.

The export now verifies each JSON and WAV independently. A missing file is created by an atomic write and an existing file is checked against its digest. A conflict stops publication of the current pointer. A failed reprocess leaves the current report empty and links the previous successful result as historical. A new ASR delivery requires a recorded provider rejection and its own authorization. This review inspected the code and saved runs. It made no fresh submissions.
