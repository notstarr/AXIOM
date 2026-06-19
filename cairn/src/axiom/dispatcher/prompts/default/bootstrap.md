# Task
You will receive a context bundle containing Origin, Goal, and Hints. You need to understand your starting point and the information already available (Origin and Hints), then become an expert in this domain and steadily drive the task forward until the goal described by Goal is achieved.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

If you have confirmed useful initial intelligence but Goal is not yet satisfied, return:
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

Only return the following after you have confirmed that Goal has been satisfied:
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```

# Rules
- If the problem is not yet solved but you already have a confirmed, helpful starting result, return the `fact`-only shape instead of rejecting.
- If the problem is not yet solved and you do not have any confirmed useful result yet, keep working and do not stop on your own.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this keep-working rule immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.
- Output `complete` only if Goal has already been definitively achieved in this session. If Goal is not yet achieved, do not summarize partial progress as completion.
- `fact.description` must clearly state the confirmed key objective results. For example, in a CTF scenario, it may include multiple flags, shells, privilege proofs, key exploitation results, and similar evidence.
- `complete.description` should explain why the currently confirmed results are sufficient to prove that Goal has been achieved.
- Use rejection only for true refusal cases. Do not reject merely because Goal is not finished yet.
- Do not put long data blobs in `description`. Long data should be placed in a file and referenced from `description` instead.
- If you are told to use reasoning, always output your final answer as a raw JSON object in the text (not inside reasoning). Do not output only reasoning with an empty text response.

# Context
## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Blackboard
```
{blackboard}
```

## Hints
```
{hints}
```
