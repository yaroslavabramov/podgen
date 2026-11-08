"""Serialize synthetic work using the author form offered by this specific request."""

import json


def author_response(body, output):
    if set(body["text"]["format"]["schema"].get("properties", {})) == {"result"}:
        output = {"result": output}
    return json.dumps(output, ensure_ascii=False)


def read_work(body, text):
    data = json.loads(text)
    return (
        data["result"] if set(body["text"]["format"]["schema"].get("properties", {})) == {"result"} else data
    )
