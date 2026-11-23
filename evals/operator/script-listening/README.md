# Blind reading of short scripts

Five pairs test the script editor's ear for order, redundant explanation, spoken language and endings. Four stories are fictional. The fifth draws on the Hampstead material from the Snow dossier. Each pair uses the same facts. The experiment's editor prepared the choices and rationales in [pairs.ru.json](pairs.ru.json). These judgments have not been independently rated by a human panel.

## Prepare

Use a separate checkout of `af02f13d` with its backend dependencies. Both optional full scripts and all ten short requests were reproduced offline at that revision. With the current production projection, the historical preparer stops at its cold-reader boundary check. The [paired texts and judgments](pairs.ru.json) can also be used for manual reading.

Supply the complete JSON profile for the script reader, and a fresh output directory.

```sh
uv run --offline --project backend python evals/operator/script-listening/prepare.py \
  /path/to/ModelProfile.json /path/to/blind-reading
```

The [preparer](prepare.py) compiles ten independent requests and shuffles them. The operator key stays separate. Each request contains one Russian story, its audience, genre, format and a target of 55 seconds within a 30–85 second range. The alternate version, source facts, case labels and expected result stay outside the request. Preparation makes no paid calls.

Optional full-script controls reuse the same reader:

- `--include-episode` adds the [1,700-word Snow script](inputs/snow.md) with its original duration. A substantive revision is expected. Check whether the reviewer identifies the order of the Hampstead discovery and the repeated conclusions.
- `--include-wright` adds the [Wright brothers script](inputs/wright.md) with its original audience, genre and duration. Consider whether the proposed edits preserve Daniels's return, the four flights and the closing words of the telegram. A specific local correction may improve this control.

## Read the results

A strict pair succeeds when the preferred version gets `keep`, the weaker version gets `revise`, and the required finding identifies the expected defect. Inspect the quotation, diagnosis and scale of the remedy. Count a correct verdict reached for another reason separately. If a control proves unsound, preserve the original outcome and explain the correction.

For complete Snow variants, read without the plan labels. Record where the first meaningful discovery occurs, how the text handles uncertainty, chronology, spoken Russian and the ending. Test a rewrite against its actual reviews separately. When the normal reader accepts a weak script, record the miss. A rewrite driven by a separate editorial diagnosis is a controlled intervention.
